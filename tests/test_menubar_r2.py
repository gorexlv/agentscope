"""T12-r2: keep infrastructure out of sessions without losing independent CLIs."""
import time
import unittest
from unittest.mock import patch
from agentscope_lib import agents, sessions


class R2Tests(unittest.TestCase):
    def collect(self, procs, cwds, index):
        with patch.object(agents.procstat, 'scan_processes', return_value=procs), \
             patch.object(agents.procstat, 'load_cwds', return_value=cwds), \
             patch.object(agents, 'build_index', return_value=index), \
             patch.object(agents.procstat, 'cpu_snapshot', return_value={}), \
             patch.object(agents.procstat, 'cpu_percent', return_value={}), \
             patch.object(agents.procstat, 'footprint_many', return_value={p: 100 for p in procs}), \
             patch.object(agents.mcp, 'collect', return_value=[]), \
             patch.object(agents.activity, 'state', return_value={}):
            return agents.collect()

    def test_review_machine_sample_has_one_host_plus_real_session(self):
        host = '/Applications/ChatGPT.app/Contents/'
        cli = host + 'Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'
        helper = host + 'Frameworks/Codex.framework/Helpers/'
        procs = {
            80342: dict(path=host+'MacOS/ChatGPT', comm='ChatGPT', ppid=1),
            80410: dict(path=helper+'Codex (Aperitif).app/Contents/MacOS/Codex', comm='Codex', ppid=80342),
            80401: dict(path=helper+'browser_crashpad_handler', comm='browser_crashpa', ppid=1),
            80403: dict(path=helper+'browser_crashpad_handler', comm='browser_crashpa', ppid=1),
            80567: dict(path=cli, comm='codex', ppid=80342, cmdline='codex --config key=value app-server'),
            80864: dict(path='/Users/test/Library/Application Support/Codex/runtime/bin/node', comm='node', ppid=80342),
            80778: dict(path='/Users/test/.codex/computer-use/Codex Computer Use.app/Contents/MacOS/SkyComputerUseService', comm='SkyComputerUseS', ppid=80342),
            80892: dict(path=cli, comm='codex', ppid=80342, cmdline='codex'),
        }
        cwds = {p: '/' for p in procs}; cwds[80892] = '/work/new-chat-2'
        data = dict(session_id='real-chat', cwd=cwds[80892], model='gpt', input_tokens=1,
                    output_tokens=1, cache_create_tokens=0, cache_read_tokens=0, context_pct=93)
        index = {'codex': [dict(path='/log/real', session_id='real-chat', cwd=cwds[80892], data=data, mtime=time.time())]}
        rows = self.collect(procs, cwds, index)
        self.assertEqual(len(rows), 2)
        by_pid = {r['pid']: r for r in rows}
        self.assertEqual(by_pid[80342]['pids'], [80342, 80401, 80403, 80410, 80567])
        self.assertEqual(by_pid[80342]['processes'], 5)
        self.assertEqual(by_pid[80342]['auxiliary_processes'], 4)
        self.assertEqual(by_pid[80342]['mem_mb'], 500)
        self.assertEqual(by_pid[80892]['pids'], [80892])
        self.assertEqual(by_pid[80892]['context_pct'], 93)
        self.assertEqual(agents.totals(rows)['sessions'], 2)
        self.assertEqual(agents.aggregate_sessions(rows), rows)

    def test_path_substrings_do_not_identify_node_or_computer_use_as_codex(self):
        for path, comm in [('/work/codex-project/bin/node', 'node'),
                           ('/Users/test/Library/Application Support/Codex/runtime/bin/node', 'node'),
                           ('/Users/test/.codex/computer-use/Codex Computer Use.app/Contents/MacOS/SkyComputerUseService', 'SkyComputerUseS')]:
            self.assertIsNone(agents.detect_agent_type(path, comm))
        self.assertEqual(agents.detect_agent_type('/usr/local/bin/codex', 'codex'), 'codex')

    def test_unowned_helper_and_app_server_are_not_sessions(self):
        rows = self.collect({1: dict(path='/bin/Codex Helper', comm='Codex Helper', ppid=0),
                             2: dict(path='/bin/codex', comm='codex', ppid=0, cmdline='codex app-server'),
                             3: dict(path='/bin/codex', comm='codex', ppid=0, cmdline='codex exec app-server'),
                             4: dict(path='/bin/codex', comm='codex', ppid=0, cmdline='codex app-server')},
                            {1: '/unknown', 2: '/', 3: '/', 4: '/work/session'}, {})
        self.assertEqual([r['pid'] for r in rows], [3, 4])

    def test_single_idle_claude_keeps_old_log_even_with_other_historical_logs(self):
        procs = {10: dict(path='/opt/claude-code/bin/claude', comm='claude', ppid=1)}
        now = time.time()
        def log(sid, age):
            return dict(path='/logs/'+sid, session_id=sid, project=sessions.cwd_to_slug('/work'),
                        mtime=now-age, cwd='/work', data=dict(session_id=sid, model='claude-sonnet-4-5',
                        cwd='/work', input_tokens=100, output_tokens=10, cache_create_tokens=0, cache_read_tokens=0))
        for logs in [[log('idle', 10800)], [log('old', 86400), log('idle', 10800)]]:
            rows = self.collect(procs, {10: '/work'}, {'claude': logs})
            self.assertEqual(rows[0]['session_id'], 'idle')
            self.assertEqual(rows[0]['session_path'], '/logs/idle')
            with patch.object(agents.os.path, 'getmtime', return_value=now-10800):
                alerts = agents.alerts(rows)
            self.assertEqual([a['kind'] for a in alerts], ['IDLE'])

    def test_shared_cwd_still_rejects_unmatched_old_log(self):
        now = time.time()
        index = {'claude': [dict(path='/logs/old', session_id='old', project=sessions.cwd_to_slug('/work'), mtime=now-10800)]}
        self.assertIsNone(agents._match_session('claude', '/work', index, disambiguate=True))
        self.assertEqual(agents._match_session('claude', '/work', index)['session_id'], 'old')
