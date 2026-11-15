import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agentscope_lib import cleanup, config, history, procstat
from test_hardening import process


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, AGENTSCOPE_CONFIG=self.tmp.name+'/config',
                         AGENTSCOPE_STATE_DIR=self.tmp.name+'/state', AGENTSCOPE_WORKSPACES='')
        env.start()
        self.addCleanup(env.stop)
        self.info = {100: process(100, 99, 'zsh'), 101: process(101, 100)}
        self.cfg = config.load_config()
        self.cfg['workspaces'] = ['/work']

    def group(self, **kw):
        return cleanup.classify_group(100, [100,101], self.info,
            cwd_query=lambda p:'/work', ports_query=lambda:{}, tty_query=lambda:set(),
            app_query=lambda p:set(), cfg=self.cfg, **kw)

    def test_invisible_parent_permission_denied_is_alive(self):
        with patch.object(cleanup.os, 'kill', side_effect=PermissionError):
            self.assertFalse(self.group()['orphan'])

    def test_invalid_config_read_fallback(self):
        Path(os.environ['AGENTSCOPE_CONFIG']).write_text('{')
        cfg = config.load_config()
        self.assertFalse(cfg['valid'])
        self.assertFalse(cfg['notify']['enabled'])

    def test_audit_failure_returns_false(self):
        with patch.object(history.os, 'makedirs', side_effect=OSError('disk full')):
            self.assertFalse(history.append('kills', {}))

    def test_ports_failure_unknown(self):
        with patch.object(procstat.subprocess, 'run', side_effect=OSError):
            self.assertIsNone(procstat.load_listening_ports())

    def test_orca_workspace_is_not_default_protection(self):
        b = process(100, 1)
        b['cmdline'] = 'node /Users/me/orca/workspaces/project/vite'
        self.assertFalse(config.protected(b, '/Users/me/orca/workspaces/project', self.cfg))

    def test_ports_all_addresses_and_no_match(self):
        import types
        for code, out, err, expected in [(0, 'p123\nn192.168.1.5:3000\n', '', {123:['192.168.1.5:3000']}),
                                         (1, '', '', {}), (1, '', 'permission denied', None),
                                         (2, '', '', None)]:
            with patch.object(procstat.subprocess, 'run', return_value=types.SimpleNamespace(returncode=code, stdout=out, stderr=err)):
                self.assertEqual(procstat.load_listening_ports(), expected)

    def test_ports_unknown_blocks_cleanup(self):
        self.info[100]['ppid'] = 1
        g = self.group()
        g['ports_unknown'] = True
        with patch.object(cleanup, 'my_protected_pids', return_value=set()):
            self.assertFalse(cleanup.cleanable(g, self.info))

    def test_invalid_config_cleanup_skips_and_prices_still_work(self):
        from agentscope_lib import sessions
        Path(os.environ['AGENTSCOPE_CONFIG']).write_text('{')
        with patch.object(cleanup, 'revalidate_group') as validate, patch.object(cleanup.os, 'kill') as kill:
            self.assertFalse(cleanup.kill_group(100))
            validate.assert_not_called()
            kill.assert_not_called()
        self.assertEqual(history.read(['kills'])[0]['reason'], 'invalid config')
        self.assertIsNotNone(sessions.pricing_for('gpt-5'))

    def test_watch_disabled_notifications_and_round_recovery(self):
        from agentscope_lib import agents
        import types
        with patch.object(cleanup, 'analyze', side_effect=[OSError('transient'), ({}, [])]) as analyze, patch.object(agents, 'collect') as collect, patch.object(cleanup.time, 'sleep', side_effect=[None, KeyboardInterrupt]):
            self.assertEqual(cleanup.cmd_watch(types.SimpleNamespace(interval=1, dry_run=True)), 0)
            self.assertEqual(analyze.call_count, 2)
            collect.assert_not_called()

    def test_watch_notification_failure_isolated(self):
        from agentscope_lib import agents
        import types
        Path(os.environ['AGENTSCOPE_CONFIG']).write_text('{"notify":{"enabled":true}}')
        with patch.object(cleanup, 'analyze', return_value=({}, [])), patch.object(agents, 'collect', side_effect=ValueError('bad session')), patch.object(cleanup.time, 'sleep', side_effect=KeyboardInterrupt):
            self.assertEqual(cleanup.cmd_watch(types.SimpleNamespace(interval=1, dry_run=True)), 0)

    def test_panel_exception_and_signal_restore(self):
        from agentscope_lib import panel
        import signal
        import types
        from unittest.mock import Mock
        args = types.SimpleNamespace(refresh=3, context_limit=200000, node=False)
        original = signal.getsignal(signal.SIGTERM)
        def terminate(timeout):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        for effect, error in [(RuntimeError('input error'), RuntimeError), (terminate, SystemExit)]:
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(panel.sys, 'stdin', Mock()))
                stack.enter_context(patch.object(panel.sys, 'stdout', io.StringIO()))
                stack.enter_context(patch.object(panel.termios, 'tcgetattr', return_value=['saved']))
                restore = stack.enter_context(patch.object(panel.termios, 'tcsetattr'))
                stack.enter_context(patch.object(panel.tty, 'setraw'))
                worker = stack.enter_context(patch.object(panel, 'PanelWorker')).return_value
                worker.poll.return_value = None
                stack.enter_context(patch.object(panel, 'read_key', side_effect=effect))
                with self.assertRaises(error):
                    panel.run(args)
                restore.assert_called_once()
                worker.close.assert_called_once()
            self.assertEqual(signal.getsignal(signal.SIGTERM), original)

    def test_hook_budget_and_interrupt_write_summary(self):
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader('review_hook', 'bin/agentscope-hook')
        spec = importlib.util.spec_from_loader(loader.name, loader)
        hook = importlib.util.module_from_spec(spec)
        loader.exec_module(hook)
        with patch.object(hook.time, 'monotonic', return_value=26), patch.object(cleanup, 'kill_group') as kill:
            summary = {}
            self.assertEqual(hook.cleanup([{'pgid': 100}], deadline=25, summary=summary), 0)
            self.assertEqual(summary['budget_skipped_groups'], 1)
            kill.assert_not_called()
        for effect in (TimeoutError('budget'), SystemExit(143)):
            with patch.object(hook.sys, 'argv', ['hook', 'session-end']), patch.object(hook, 'read_stdin_json', return_value={}), patch.object(hook, 'find_session_groups', side_effect=effect):
                if isinstance(effect, SystemExit):
                    with self.assertRaises(SystemExit):
                        hook.main()
                else:
                    self.assertEqual(hook.main(), 0)
        self.assertEqual([r['result'] for r in history.read(['sessions'])], ['deadline exceeded', 'interrupted'])

    def test_child_churn_only_signals_confirmed_identities(self):
        self.info[100]['ppid'] = 1
        expected = self.group()
        self.info[102] = process(102, 100)
        g = dict(expected, members=[100,101,102])
        alive = dict(self.info)
        with patch.object(cleanup, 'revalidate_group', return_value=(self.info,g)), patch.object(cleanup, 'my_protected_pids', return_value=set()), patch.object(cleanup, 'proc_cwd', return_value='/work'), patch.object(cleanup, 'proc_bsdinfo', side_effect=lambda p:alive.get(p)), patch.object(cleanup, 'proc_pgid', return_value=100), patch.object(cleanup, 'parent_exited', return_value=True), patch.object(cleanup, 'scan_processes', return_value={102:self.info[102]}), patch.object(cleanup.os, 'kill', side_effect=lambda p,s:alive.pop(p,None)) as kill:
            self.assertFalse(cleanup.kill_group(100, expected=expected, timeout=0))
            self.assertEqual([c.args[0] for c in kill.call_args_list], [101,100])
        self.assertEqual(history.read(['kills'])[-1]['result'], 'remaining')

    def test_panel_input_responsive_during_slow_collection(self):
        import threading
        import time
        import types
        from unittest.mock import Mock
        from agentscope_lib import panel, panel_worker
        started, release = threading.Event(), threading.Event()
        workers = []
        real_worker = panel_worker.PanelWorker
        def slow_analyze(**kwargs):
            started.set()
            release.wait(2)
            return {}, []
        def make_worker(args):
            worker = real_worker(args)
            workers.append(worker)
            self.assertTrue(started.wait(1))
            return worker
        args = types.SimpleNamespace(refresh=1, context_limit=200000, node=False)
        try:
            with patch.object(panel.cleanup, 'analyze', side_effect=slow_analyze), patch.object(panel_worker.agents, 'collect', return_value=[]), patch.object(panel.sys, 'stdin', Mock()), patch.object(panel.sys, 'stdout', io.StringIO()), patch.object(panel.termios, 'tcgetattr', return_value=[]), patch.object(panel.termios, 'tcsetattr'), patch.object(panel.tty, 'setraw'), patch.object(panel, 'PanelWorker', side_effect=make_worker), patch.object(panel, 'read_key', return_value='q'):
                start = time.monotonic()
                self.assertEqual(panel.run(args), 0)
                self.assertLess(time.monotonic()-start, .5)
        finally:
            release.set()
            for worker in workers:
                worker.close()
                worker.thread.join(2)

    def test_config_permission_failure_and_revision_cache(self):
        path = Path(os.environ['AGENTSCOPE_CONFIG'])
        path.write_text('{}')
        original = config._parse_config
        with patch.object(config, '_parse_config', wraps=original) as parse:
            config.load_config()
            config.load_config()
            self.assertEqual(parse.call_count, 1)
        path.write_text('{"notify":{"enabled":true}}')
        with patch('builtins.open', side_effect=PermissionError('denied')):
            result = config.load_config()
            self.assertFalse(result['valid'])
            self.assertFalse(result['notify']['enabled'])
