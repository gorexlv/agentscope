"""Regression coverage for T12 session identity and its CLI/notification consumers."""
import contextlib
import io
import json
import os
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from agentscope_lib import agents, activity, cli, notify, ownership, sessions


class ReviewTests(unittest.TestCase):
    def test_framework_helpers_are_explicit_but_project_names_are_not(self):
        self.assertFalse(agents._is_session_main({'comm': 'Codex', 'path': '/Applications/ChatGPT.app/Contents/Frameworks/Codex.framework/Helpers/Codex.app/Contents/MacOS/Codex'}))
        self.assertTrue(agents._is_session_main({'comm': 'codex', 'path': '/work/helper-project/bin/codex'}))

    def collect(self, procs, cwds, logs):
        with patch.object(agents.procstat, 'scan_processes', return_value=procs), \
             patch.object(agents.procstat, 'load_cwds', return_value=cwds), \
             patch.object(agents, 'build_index', return_value={'claude': logs}), \
             patch.object(agents.mcp, 'collect', return_value=[]), \
             patch.object(agents.activity, 'state', return_value={}):
            return agents.collect(with_resources=False)

    def proc(self, parent=1, start=0, desktop=False):
        return dict(ppid=parent, start_time=start * 1000000,
                    path='/Applications/Claude.app/Contents/MacOS/Claude' if desktop else '/opt/claude-code/bin/claude',
                    comm='Claude' if desktop else 'claude')

    def log(self, sid, cwd, start=None, age=0):
        return dict(path='/logs/' + sid, session_id=sid, mtime=time.time() - age,
                    start_time=start, project=sessions.cwd_to_slug(cwd), cwd=cwd,
                    data=dict(session_id=sid, cwd=cwd, model='claude-sonnet-4-5',
                              input_tokens=100, output_tokens=10, cache_create_tokens=0, cache_read_tokens=0))

    def test_claude_desktop_does_not_swallow_two_code_sessions(self):
        procs = {10: self.proc(desktop=True), 21: self.proc(10), 22: self.proc(10)}
        rows = self.collect(procs, {10: '/', 21: '/work/a', 22: '/work/b'},
                            [self.log('a', '/work/a'), self.log('b', '/work/b')])
        self.assertEqual({r['session_id'] for r in rows if r.get('session_id')}, {'a', 'b'})
        self.assertEqual(next(r for r in rows if r['pid'] == 21)['pids'], [21])
        self.assertEqual(next(r for r in rows if r['pid'] == 22)['pids'], [22])

    def test_nested_cli_and_raw_aggregation_remain_independent(self):
        rows = self.collect({30: self.proc(), 31: self.proc(30)}, {30: '/work/c', 31: '/work/d'},
                            [self.log('c', '/work/c'), self.log('d', '/work/d')])
        self.assertEqual({r['session_id'] for r in rows}, {'c', 'd'})
        raw = [dict(pid=1, agent_type='claude', is_main=True),
               dict(pid=2, ppid=1, agent_type='claude', is_main=True)]
        self.assertEqual(len(agents.aggregate_sessions(raw)), 2)

    def test_same_cwd_logs_use_start_time_not_pid_or_latest_order(self):
        now = time.time()
        rows = self.collect({42: self.proc(start=now - 20), 21: self.proc(start=now - 10)},
                            {42: '/work', 21: '/work'},
                            [self.log('new', '/work', now - 10), self.log('old', '/work', now - 20)])
        self.assertEqual({r['pid']: r['session_id'] for r in rows}, {42: 'old', 21: 'new'})

    def test_same_cwd_does_not_reuse_or_borrow_stale_logs(self):
        rows = self.collect({1: self.proc(), 2: self.proc()}, {1: '/work', 2: '/work'},
                            [self.log('fresh', '/work'), self.log('stale', '/work', age=90000)])
        self.assertEqual(sum(bool(r.get('session_path')) for r in rows), 1)
        self.assertNotIn('stale', [r.get('session_id') for r in rows])
        self.assertEqual(len(rows), 2)

    def test_claude_inferred_context_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.jsonl'
            for model, expected in [('claude-sonnet-4-5', 10), ('claude-opus-5-5', 82),
                                    ('claude-sonnet-4-5[1m]', 82), (None, None), ('unknown', None)]:
                path.write_text(json.dumps({'message': {'model': model, 'usage': {'input_tokens': 180000}}}))
                result = sessions.parse_claude_session(str(path))
                self.assertEqual(result['context_pct'], expected)
                self.assertEqual(sessions.parse_claude_session(str(path), 400000)['context_pct'], 55)
            path.write_text('{}')
            self.assertIsNone(sessions.parse_claude_session(str(path))['context_pct'])

    def test_notifications_new_session_and_pid_reuse(self):
        config = {'notify': {'enabled': True, 'cooldown_minutes': 10}}
        n = notify.Notifier()
        a = dict(pid=1, session_id='a', kind='CONTEXT_LOW', level='warn', msg='session a')
        b = dict(pid=2, session_id='b', kind='CONTEXT_LOW', level='warn', msg='session b')
        with patch.object(notify.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)) as run:
            n.send(agents.aggregate_alerts([a]), config)
            n.send(agents.aggregate_alerts([a, b]), config)
            n.send(agents.aggregate_alerts([dict(a, pid=3), b]), config)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args.args[0][-1], 'session b')
            n.send([dict(a, session_id='new-session')], config)
            self.assertEqual(run.call_count, 3)
            self.assertIn(('b', 'CONTEXT_LOW'), n.sent)

    def test_whoami_chooses_nearest_member_and_owner_returns_session_pid(self):
        rows = [dict(pid=10, pids=[10, 11], session_id='outer'),
                dict(pid=20, pids=[20, 21], session_id='inner')]
        parents = {100: 21, 21: 11, 11: 1}
        out = io.StringIO()
        with patch.object(cli.os, 'getpid', return_value=100), \
             patch.object(cli.procstat, 'proc_parent', side_effect=lambda p: parents.get(p, 1)), \
             patch.object(cli.agents_mod, 'collect', return_value=rows), contextlib.redirect_stdout(out):
            cli.cmd_whoami(types.SimpleNamespace(json=True))
        result = json.loads(out.getvalue())
        self.assertEqual(result['session']['session_id'], 'inner')
        self.assertEqual(result['match_confidence'], 'ppid')
        self.assertEqual(ownership.owner_pids(100, {p: {'ppid': v} for p, v in parents.items()}, rows), ([20], 'ancestor'))

    def test_conflicts_exclude_generic_directories_and_retain_cwds(self):
        rows = [dict(pid=i, cwd=cwd) for i, cwd in enumerate(['/', '/', os.path.expanduser('~'), os.path.expanduser('~')])]
        self.assertEqual(activity.conflicts(rows), [])
        rows = [dict(pid=i, cwd='/a' if i < 2 else '/b') for i in range(4)]
        alert = agents.alerts(rows)[0]
        self.assertEqual(alert['count'], 4)
        self.assertEqual(alert['cwds'], ['/a', '/b'])
        self.assertIn('2 个目录', alert['msg'])
        self.assertEqual(agents.project_name(os.path.expanduser('~')), '主目录')
