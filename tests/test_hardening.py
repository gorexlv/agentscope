import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch, Mock

from agentscope_lib import cleanup, config, history, notify, sessions, agents, panel


def process(pid, ppid, comm='node', pgid=100, start=123):
    return dict(pid=pid, ppid=ppid, comm=comm, name=comm, pgid=pgid,
                start_time=start, uid=os.getuid(), cmdline=comm)


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'AGENTSCOPE_CONFIG': self.temp.name + '/config.json',
                                         'AGENTSCOPE_STATE_DIR': self.temp.name + '/state',
                                         'AGENTSCOPE_WORKSPACES': ''})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cfg = config.load_config()
        self.cfg['workspaces'] = ['/work']
        self.info = {100: process(100, 1, 'zsh'), 101: process(101, 100)}

    def classify(self, **kwargs):
        params = dict(cwd_query=lambda p: '/work/repo', ports_query=lambda: {},
                      tty_query=lambda: set(), app_query=lambda p: set(), cfg=self.cfg)
        params.update(kwargs)
        return cleanup.classify_group(100, [100, 101], self.info, **params)

    def test_parent_alive_is_not_orphan(self):
        self.info[100]['ppid'] = 99
        self.info[99] = process(99, 1, 'zsh', 99)
        self.assertFalse(self.classify()['orphan'])

    def test_parent_absent_and_init_are_orphans(self):
        self.assertTrue(self.classify()['orphan'])
        self.info[100]['ppid'] = 99
        self.assertTrue(self.classify()['orphan'])

    def test_every_root_must_be_orphan_workspace_tool(self):
        self.info[101]['ppid'] = 99
        self.info[99] = process(99, 1)
        self.assertFalse(self.classify()['orphan'])
        del self.info[99]
        self.assertFalse(self.classify(cwd_query=lambda p: '/work' if p == 100 else '/tmp')['orphan'])

    def test_workspace_boundaries(self):
        self.assertTrue(config.within('/work/repo', ['/work']))
        self.assertTrue(config.within('/work', ['/work']))
        self.assertFalse(config.within('/workspace', ['/work']))
        self.assertFalse(config.within('/work/../other', ['/work']))

    def test_protection_fields_substring_and_glob(self):
        for field, pattern, value in [('comm', 'node', 'node'), ('cmdline', '*--keep*', 'node --keep'), ('cwd', '/work', '/work/repo')]:
            cfg = dict(self.cfg, protect={field: [pattern]})
            b = dict(self.info[100], **{field: value}) if field != 'cwd' else self.info[100]
            self.assertTrue(config.protected(b, '/work/repo', cfg))
        self.cfg['protect'] = {'comm': ['node']}
        self.assertTrue(self.classify()['protected'])

    def test_desktop_agent_and_ports(self):
        self.assertTrue(self.classify(app_query=lambda p: {'Orca'})['protected'])
        self.info[101]['comm'] = 'claude'
        self.assertTrue(self.classify()['has_live_session'])
        self.assertEqual(self.classify(ports_query=lambda: {101: ['*:1234']})['ports'], ['*:1234'])

    def test_current_tree_protection(self):
        info = {20: process(20, 10, pgid=20), 10: process(10, 1, pgid=10),
                21: process(21, 20, pgid=21), 30: process(30, 1, pgid=30)}
        with patch.object(cleanup.os, 'getpid', return_value=20), patch.object(cleanup, 'proc_cwd', return_value=''):
            self.assertEqual(cleanup.my_protected_pids(info), {10, 20, 21})

    def test_seen_expires_and_reused_group_is_new(self):
        g = self.classify()
        seen = {notify.group_key(g)}
        notify.expire_seen(seen, [g])
        self.assertEqual(len(seen), 1)
        self.info[100]['start_time'] += 1
        self.assertNotIn(notify.group_key(self.classify()), seen)
        notify.expire_seen(seen, [])
        self.assertFalse(seen)

    def test_config_defaults_overrides_and_invalid(self):
        self.assertIn(os.path.expanduser('~/Workbuddy'), config.load_config()['workspaces'])
        p = Path(os.environ['AGENTSCOPE_CONFIG'])
        p.write_text(json.dumps({'workspaces': ['/custom'], 'notify': {'enabled': True}}))
        self.assertEqual(config.load_config()['workspaces'], ['/custom'])
        with patch.dict(os.environ, {'AGENTSCOPE_WORKSPACES': '/env:/env2'}):
            self.assertEqual(config.load_config()['workspaces'], ['/env', '/env2'])
        for bad in ['{', '[]', '{"protect":{"cwd": "x"}}', '{"notify":{"cooldown_minutes": -1}}', '{"pricing":{"x":{"input":1}}}']:
            p.write_text(bad)
            with self.assertRaises(ValueError):
                config.load_config()

    def test_pricing_exact_override_and_fallback(self):
        self.assertFalse(sessions.cost_estimated('gpt-5'))
        self.assertTrue(sessions.cost_estimated('new-model'))
        self.assertIsNotNone(sessions.pricing_for('new-model'))
        rates = dict(input=1, output=2, cacheWrite=3, cacheRead=4)
        Path(os.environ['AGENTSCOPE_CONFIG']).write_text(json.dumps({'pricing': {'new-model': rates}}))
        self.assertEqual(sessions.pricing_for('new-model'), rates)
        self.assertFalse(sessions.cost_estimated('new-model'))
        self.assertEqual(sessions.calculate_cost('new-model', 1000000, 1000000), 3)
        self.assertTrue(sessions.format_cost(1, True).startswith('≈'))

    def test_history_filter_and_partial_lines(self):
        history.append('kills', {'result': 'dry-run'})
        history.append('sessions', {'session_id': 'test'})
        with open(Path(history.state_dir()) / 'kills.jsonl', 'a') as f:
            f.write('{partial\n')
        self.assertEqual(len(history.read(['kills', 'sessions'], 1)), 2)
        self.assertEqual(len(history.read(['sessions'])), 1)

    def test_notify_filter_cooldown_and_disabled(self):
        n = notify.Notifier()
        alerts = [{'pid': 1, 'kind': 'CONTEXT_LOW', 'msg': '"\\ malicious'}, {'pid': 1, 'kind': 'IDLE', 'msg': 'idle'}]
        with patch.object(notify.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)) as run:
            n.send(alerts)
            run.assert_not_called()
            Path(os.environ['AGENTSCOPE_CONFIG']).write_text('{"notify":{"enabled":true,"cooldown_minutes":1}}')
            with patch.object(notify.time, 'monotonic', return_value=10):
                n.send(alerts)
                n.send(alerts)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][-1], alerts[0]['msg'])
            with patch.object(notify.time, 'monotonic', return_value=71):
                n.send(alerts)
            self.assertEqual(run.call_count, 2)

    def test_kill_rejects_protected_and_changed_members(self):
        g = self.classify()
        with patch.object(cleanup, 'analyze', return_value=(self.info, [g])), patch.object(cleanup, 'my_protected_pids', return_value={101}), patch.object(cleanup.os, 'kill') as kill:
            self.assertFalse(cleanup.kill_group(100))
            kill.assert_not_called()
        expected = dict(g, identities={'100': 1})
        with patch.object(cleanup, 'analyze', return_value=(self.info, [g])), patch.object(cleanup, 'my_protected_pids', return_value=set()), patch.object(cleanup.os, 'kill') as kill:
            self.assertFalse(cleanup.kill_group(100, expected=expected))
            self.assertTrue(cleanup.kill_group(100, dry_run=True))
            kill.assert_not_called()

    def test_kill_audit_and_no_pid_reuse(self):
        g = self.classify()
        with patch.object(cleanup, 'analyze', return_value=(self.info, [g])), patch.object(cleanup, 'my_protected_pids', return_value=set()), patch.object(cleanup, 'proc_cwd', return_value='/work'), patch.object(cleanup, 'proc_pgid', return_value=100), patch.object(cleanup, 'proc_bsdinfo', return_value=process(100, 1, start=999)), patch.object(cleanup.os, 'kill') as kill:
            self.assertFalse(cleanup.kill_group(100, timeout=0))
            kill.assert_not_called()
        self.assertEqual([r['result'] for r in history.read(['kills'])], ['attempt', 'gone'])

    def test_yes_dry_run_never_signals(self):
        g = self.classify()
        args = types.SimpleNamespace(yes=True, dry_run=True, keep_ports=True)
        with patch.object(cleanup, 'analyze', return_value=(self.info, [g])), patch.object(cleanup, 'my_protected_pids', return_value=set()), patch.object(cleanup, 'kill_group') as kill, contextlib.redirect_stdout(io.StringIO()):
            cleanup.cmd_clean(args)
        self.assertTrue(kill.call_args.kwargs['dry_run'])

    def test_panel_tree(self):
        lines = panel.tree_lines(self.classify(), self.info)
        self.assertIn('100 zsh', lines[0])
        self.assertTrue(lines[1].startswith('    '))

    def test_panel_confirmation_and_restore(self):
        g = self.classify()
        fake_stdin = Mock()
        fake_stdin.fileno.return_value = 0
        args = types.SimpleNamespace(refresh=3, context_limit=200000, node=False)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(panel.sys, 'stdin', fake_stdin))
            stack.enter_context(patch.object(panel.sys, 'stdout', io.StringIO()))
            stack.enter_context(patch.object(panel.termios, 'tcgetattr', return_value=['saved']))
            restore = stack.enter_context(patch.object(panel.termios, 'tcsetattr'))
            stack.enter_context(patch.object(panel.tty, 'setraw'))
            stack.enter_context(patch.object(panel, 'read_key', side_effect=['c', 'n', 's', 'c', 'y', 'q']))
            stack.enter_context(patch.object(panel.cleanup, 'analyze', return_value=(self.info, [g])))
            stack.enter_context(patch.object(panel.cleanup, 'cleanable', return_value=True))
            kill = stack.enter_context(patch.object(panel.cleanup, 'kill_group', return_value=True))
            stack.enter_context(patch.object(panel.agents, 'collect', return_value=[]))
            self.assertEqual(panel.run(args), 0)
            kill.assert_called_once_with(100, mode='panel', expected=g)
            restore.assert_called_once()

    def test_hook_records_empty_session_and_uses_shared_cleanup(self):
        loader = importlib.machinery.SourceFileLoader('hook_test', 'bin/agentscope-hook')
        spec = importlib.util.spec_from_loader(loader.name, loader)
        hook = importlib.util.module_from_spec(spec)
        loader.exec_module(hook)
        with patch.object(hook.sys, 'argv', ['hook', 'session-end']), patch.object(hook, 'read_stdin_json', return_value={'session_id': 's1', 'cwd': '/work'}), patch.object(hook, 'find_session_groups', return_value=[]):
            self.assertEqual(hook.main(), 0)
        self.assertEqual(history.read(['sessions'])[0]['cleaned_processes'], 0)
        g = self.classify()
        with patch.object(cleanup, 'kill_group', return_value=True) as kill:
            self.assertEqual(hook.cleanup([g]), 2)
            kill.assert_called_once_with(100, mode='hook', expected=g)

    def test_mocked_signals_children_first_and_audit(self):
        g = self.classify()
        alive = dict(self.info)
        def signal_pid(pid, sig):
            alive.pop(pid, None)
        with patch.object(cleanup, 'analyze', return_value=(self.info, [g])), patch.object(cleanup, 'my_protected_pids', return_value=set()), patch.object(cleanup, 'proc_cwd', return_value='/work'), patch.object(cleanup, 'proc_pgid', return_value=100), patch.object(cleanup, 'proc_bsdinfo', side_effect=lambda p: alive.get(p)), patch.object(cleanup.os, 'kill', side_effect=signal_pid) as kill:
            self.assertTrue(cleanup.kill_group(100, timeout=0))
            self.assertEqual([c.args[0] for c in kill.call_args_list], [101, 100])
        self.assertEqual([r['result'] for r in history.read(['kills'])], ['attempt', 'cleaned'])

    def test_watch_dedup_and_dry_run_notification(self):
        g = self.classify()
        args = types.SimpleNamespace(interval=1, keep_ports=True, dry_run=True)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(cleanup, 'analyze', side_effect=[(self.info, [g]), (self.info, [g]), (self.info, []), (self.info, [g])]))
            stack.enter_context(patch.object(cleanup, 'cleanable', return_value=True))
            stack.enter_context(patch.object(agents, 'collect', return_value=[]))
            stack.enter_context(patch.object(cleanup.time, 'sleep', side_effect=[None, None, None, KeyboardInterrupt]))
            notifier = stack.enter_context(patch.object(cleanup, 'Notifier'))
            kill = stack.enter_context(patch.object(cleanup, 'kill_group', return_value=True))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.assertEqual(cleanup.cmd_watch(args), 0)
            self.assertEqual(kill.call_count, 2)
            self.assertTrue(all(c.kwargs['dry_run'] for c in kill.call_args_list))
            self.assertTrue(all(c.args[0] == [] for c in notifier.return_value.send.call_args_list))

    def test_audit_failure_blocks_signals(self):
        g = self.classify()
        with patch.object(cleanup, 'analyze', return_value=(self.info, [g])), patch.object(cleanup, 'my_protected_pids', return_value=set()), patch.object(cleanup, 'proc_cwd', return_value='/work'), patch.object(cleanup, 'audit', side_effect=OSError('disk full')), patch.object(cleanup.os, 'kill') as kill:
            with self.assertRaises(OSError):
                cleanup.kill_group(100)
            kill.assert_not_called()

    def test_missing_cmdline_is_protected(self):
        b = dict(self.info[100])
        del b['cmdline']
        self.assertTrue(config.protected(b, '/work', self.cfg))

    def test_backup_retention_only_owned_files(self):
        spec = importlib.util.spec_from_file_location('installer', 'install/install.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        target = Path(self.temp.name) / 'settings.json'
        target.write_text('one')
        unrelated = Path(str(target) + '.bak.agentscope.notes')
        unrelated.write_text('keep')
        first = mod.backup(str(target))
        target.write_text('two')
        second = mod.backup(str(target))
        self.assertFalse(Path(first).exists())
        self.assertEqual(Path(second).read_text(), 'two')
        self.assertTrue(unrelated.exists())


if __name__ == '__main__':
    unittest.main()
