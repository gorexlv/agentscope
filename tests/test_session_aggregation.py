"""Session identity, resource totals and warning regressions without live processes."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentscope_lib import agents, sessions, snapshot


class AggregationTests(unittest.TestCase):
    def row(self, pid=1, **fields):
        return dict(dict(pid=pid, agent_type='codex', cwd='/missing/project',
                         mem_mb=100, cpu_pct=1, cost=0.2, is_main=True), **fields)

    def test_same_log_is_one_session_and_cost_is_not_multiplied(self):
        rows = [self.row(pid, session_id='abc', session_path='/log',
                         is_main=pid == 1) for pid in range(1, 20)]
        grouped = agents.aggregate_sessions(rows)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]['processes'], 19)
        self.assertEqual(grouped[0]['mem_mb'], 1900)
        self.assertEqual(grouped[0]['cpu_pct'], 19)
        self.assertEqual(agents.totals(rows)['cost'], 0.2)
        self.assertEqual(agents.totals(rows)['sessions'], 1)
        self.assertEqual(agents.aggregate_sessions(grouped), grouped)
        self.assertFalse(any(a['kind'] == 'CWD_CONFLICT' for a in agents.alerts(rows)))

    def test_ancestry_without_log_and_independent_sessions_in_same_directory(self):
        rows = [self.row(), self.row(2, ppid=1, is_main=False), self.row(3)]
        grouped = agents.aggregate_sessions(rows)
        self.assertEqual(len(grouped), 2)
        self.assertEqual(grouped[0]['mem_mb'], 200)
        conflict = agents.alerts(rows)[0]
        self.assertEqual(conflict['kind'], 'CWD_CONFLICT')
        self.assertEqual(conflict['count'], 2)
        self.assertEqual(conflict['pids'], [1, 3])

    def test_helpers_detected_in_executable_path(self):
        self.assertFalse(agents._is_session_main({'comm': 'Codex', 'path': '/Applications/Codex Helper.app/Contents/MacOS/Codex'}))

    def test_idle_once_per_session_and_aggregated_by_kind(self):
        rows = [self.row(1, session_id='a', session_path='/a'),
                self.row(2, session_id='a', session_path='/a'),
                self.row(3, session_id='b', session_path='/b')]
        with patch.object(agents.os.path, 'getmtime', return_value=0):
            alerts = agents.alerts(rows)
        idle = next(a for a in alerts if a['kind'] == 'IDLE')
        self.assertEqual(idle['count'], 2)
        self.assertEqual(idle['pids'], [1, 3])
        self.assertEqual(len([a for a in alerts if a['kind'] == 'IDLE']), 1)

    def test_unknown_context_does_not_warn_known_zero_does(self):
        rows = [self.row(context_pct=None), self.row(2, context_pct=0)]
        warnings = [a for a in agents.alerts(rows) if a['kind'] == 'CONTEXT_LOW']
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]['pids'], [2])

    def test_claude_unknown_window_is_not_zero_remaining(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            path.write_text(json.dumps({'message': {'model': 'claude', 'usage': {'input_tokens': 250000}}}))
            self.assertIsNone(sessions.parse_claude_session(str(path))['context_pct'])
            self.assertEqual(sessions.parse_claude_session(str(path), 1000000)['context_pct'], 75)
            self.assertEqual(sessions.parse_claude_session(str(path), 200000)['context_pct'], 0)

    def test_snapshot_additions_and_missing_resource(self):
        row = snapshot.session_rows([self.row(mem_mb=None)])[0]
        self.assertEqual(row['project'], 'project')
        self.assertEqual(row['session_id'], 'pid:1')
        self.assertEqual(row['processes'], 1)
        self.assertIsNone(row['mem_mb'])
        with patch.object(agents.subprocess, 'run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = '/repo/top\n'
            self.assertEqual(agents.project_name('/repo/top/subdir'), 'top')

    def test_embedded_desktop_host_and_reparented_helpers(self):
        procs = {
            10: {'path': '/Applications/ChatGPT.app/Contents/MacOS/ChatGPT', 'comm': 'ChatGPT', 'ppid': 1},
            11: {'path': '/Applications/ChatGPT.app/Contents/Helpers/Codex Renderer', 'comm': 'Codex', 'ppid': 10},
            12: {'path': '/Applications/ChatGPT.app/Contents/Helpers/Codex crashpad', 'comm': 'Codex', 'ppid': 1},
            13: {'path': '/bin/codex', 'comm': 'codex', 'ppid': 10},
            20: {'path': '/bin/codex', 'comm': 'codex', 'ppid': 1}}
        with patch.object(agents.procstat, 'scan_processes', return_value=procs), \
             patch.object(agents.procstat, 'load_cwds', return_value={p: '/work' for p in procs}), \
             patch.object(agents, 'build_index', return_value={}), \
             patch.object(agents.procstat, 'cpu_snapshot', return_value={}), \
             patch.object(agents.procstat, 'cpu_percent', return_value={}), \
             patch.object(agents.procstat, 'footprint_many', return_value={p: 100 for p in procs}), \
             patch.object(agents.mcp, 'collect', return_value=[]), \
             patch.object(agents.activity, 'state', return_value={}):
            rows = agents.collect()
        self.assertEqual(len(rows), 2)
        desktop = next(r for r in rows if r['pid'] == 10)
        self.assertEqual(desktop['processes'], 4)
        self.assertEqual(desktop['mem_mb'], 400)
        self.assertEqual(desktop['pids'], [10, 11, 12, 13])

    def test_collect_claims_logs_once_and_folds_helper_resources(self):
        procs = {1: {'path': '/bin/codex', 'comm': 'codex', 'ppid': 0},
                 2: {'path': '/bin/Codex Helper', 'comm': 'Codex', 'ppid': 1},
                 3: {'path': '/bin/codex', 'comm': 'codex', 'ppid': 0}}
        data = dict(model='gpt', input_tokens=2, output_tokens=1,
                    cache_create_tokens=0, cache_read_tokens=0)
        index = {'codex': [dict(path='/a', session_id='a', cwd='/work', data=data, mtime=1),
                           dict(path='/b', session_id='b', cwd='/work', data=data, mtime=1)]}
        with patch.object(agents.procstat, 'scan_processes', return_value=procs), \
             patch.object(agents.procstat, 'load_cwds', return_value={p: '/work' for p in procs}), \
             patch.object(agents, 'build_index', return_value=index), \
             patch.object(agents.mcp, 'collect', return_value=[]), \
             patch.object(agents.activity, 'state', return_value={}):
            rows = agents.collect(with_resources=False)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['session_id'] for r in rows}, {'a', 'b'})
        self.assertEqual(sorted(r['processes'] for r in rows), [1, 2])


if __name__ == '__main__':
    unittest.main()
