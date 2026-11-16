"""daemon 快照与本地 socket 的契约测试（不碰真实进程，只用临时目录）。"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentscope_lib import agents, cleanup, server as server_mod, snapshot as snapshot_mod


def fake_group(pgid=100, size=1):
    return {'pgid': pgid, 'members': [pgid], 'size': size, 'orphan': True,
            'protected': False, 'has_live_session': False, 'ports': [],
            'ports_unknown': False, 'config_valid': True, 'names': ['node'],
            'cwd': '/tmp/work', 'identities': {str(pgid): 1234.0},
            'root_starts': [(pgid, 1234.0)]}


def fake_info(pgid=100):
    return {pgid: {'pid': pgid, 'pgid': pgid, 'ppid': 1, 'comm': 'node', 'name': 'node',
                   'cmdline': 'node server.js', 'start_time': 1234.0, 'uid': os.getuid()}}


def fake_cleaner(calls):
    """记录被清理的 pgid，模拟清理成功。"""
    def clean(group):
        calls.append(group['pgid'])
        return True, 'cleaned', None
    return clean


def fake_sessions():
    return [{'pid': 42, 'agent_type': 'claude', 'is_main': True, 'model': 'claude-opus-5-5',
             'cwd': '/tmp/work', 'git_branch': 'main', 'activity_state': 'WORKING',
             'cpu_pct': 3.2, 'mem_mb': 410.0, 'context_pct': 64, 'cost': 0.42,
             'input_tokens': 100, 'output_tokens': 20},
            {'pid': 43, 'agent_type': 'claude', 'is_main': False, 'model': 'claude-opus-5-5',
             'cwd': '/tmp/work', 'git_branch': 'main', 'activity_state': 'IDLE',
             'cpu_pct': 0.0, 'mem_mb': 12.0, 'context_pct': None, 'cost': None}]


class DaemonTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config_path = self.root / 'config.json'
        self.config_path.write_text(json.dumps({'workspaces': [str(self.root)]}))
        env = patch.dict(os.environ, AGENTSCOPE_CONFIG=str(self.config_path),
                         AGENTSCOPE_WORKSPACES='', AGENTSCOPE_STATE_DIR=str(self.root / 'state'))
        env.start()
        self.addCleanup(env.stop)


class SnapshotTests(DaemonTestCase):
    def test_01_snapshot_write_is_atomic_and_private(self):
        data = snapshot_mod.build_snapshot(sessions=fake_sessions(), info={}, units=[],
                                           alerts=[], load={'load1': 1.0, 'ncpu': 8})
        path = snapshot_mod.write_snapshot(data)
        self.assertTrue(os.path.exists(path))
        self.assertEqual('0o600', oct(os.stat(path).st_mode & 0o777))
        stored = snapshot_mod.read_snapshot(path)
        self.assertEqual(stored['schema'], snapshot_mod.SCHEMA)
        # 临时文件不能留下残骸
        leftovers = [name for name in os.listdir(os.path.dirname(path)) if name != 'snapshot.json']
        self.assertEqual(leftovers, [])
        # 覆盖写：旧 inode 消失，内容始终是完整 JSON
        first = os.stat(path).st_ino
        snapshot_mod.write_snapshot(dict(data, generated_at='later'))
        self.assertNotEqual(first, os.stat(path).st_ino)
        self.assertEqual('0o600', oct(os.stat(path).st_mode & 0o777))
        self.assertEqual(snapshot_mod.read_snapshot(path)['generated_at'], 'later')

    def test_failed_replace_preserves_complete_previous_snapshot(self):
        path = snapshot_mod.write_snapshot({'schema': 1, 'generated_at': 'old'})
        with patch.object(snapshot_mod.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                snapshot_mod.write_snapshot({'schema': 1, 'generated_at': 'new'})
        self.assertEqual(snapshot_mod.read_snapshot(path)['generated_at'], 'old')
        self.assertEqual(os.listdir(os.path.dirname(path)), ['snapshot.json'])

    def test_02_snapshot_fields_match_contract(self):
        units = [fake_group(100, 2)]
        data = snapshot_mod.build_snapshot(sessions=fake_sessions(), info=fake_info(100),
                                           units=units,
                                           alerts=[{'level': 'warn', 'kind': 'CONTEXT_LOW',
                                                    'pid': 42, 'msg': '上下文剩余 10%'}],
                                           load={'load1': 2.1, 'load5': 2.4, 'load15': 2.0,
                                                 'ncpu': 12, 'mem_used_pct': 61.5,
                                                 'swap_used_gb': 1.2})
        for key in ('schema', 'generated_at', 'daemon', 'host', 'sessions', 'totals',
                    'alerts', 'orphans', 'reclaim'):
            self.assertIn(key, data)
        for key in ('pid', 'version', 'interval_s', 'last_clean_at'):
            self.assertIn(key, data['daemon'])
        for key in ('load1', 'load5', 'load15', 'ncpu', 'mem_used_pct', 'swap_used_gb'):
            self.assertIn(key, data['host'])
        session = data['sessions'][0]
        for key in ('pid', 'agent', 'model', 'cwd', 'branch', 'state', 'cpu_pct',
                    'mem_mb', 'context_pct', 'cost_usd'):
            self.assertIn(key, session)
        self.assertEqual(session['state'], 'working')
        self.assertEqual(session['agent'], 'claude')
        # 子进程不重复出现在会话列表
        self.assertEqual(len(data['sessions']), 1)
        self.assertEqual(data['totals']['sessions'], 1)
        self.assertEqual(data['totals']['tokens'], 120)
        self.assertEqual(data['orphans']['groups'], 1)
        self.assertEqual(data['orphans']['processes'], 2)
        self.assertEqual(data['orphans']['cleanable_groups'], 1)
        self.assertEqual(data['alerts'][0]['kind'], 'CONTEXT_LOW')
        for key in ('worktrees_gb', 'removable', 'updated_at'):
            self.assertIn(key, data['reclaim'])

    def test_03_state_mapping_covers_known_activity_states(self):
        self.assertEqual(snapshot_mod.session_state({'activity_state': 'WAITING_INPUT'}), 'waiting')
        self.assertEqual(snapshot_mod.session_state({'activity_state': 'WAITING_PERMISSION'}), 'waiting')
        self.assertEqual(snapshot_mod.session_state({'activity_state': 'IDLE'}), 'idle')
        self.assertEqual(snapshot_mod.session_state({'activity_state': 'SUSPECTED_LOOP'}), 'stuck')
        self.assertEqual(snapshot_mod.session_state({'activity_state': 'SUSPECTED_STALL'}), 'stuck')
        self.assertEqual(snapshot_mod.session_state({}), 'unknown')

    def test_04_reclaim_uses_cache_not_full_scan(self):
        state = self.root / 'state'
        state.mkdir(parents=True, exist_ok=True)
        (state / 'worktree-sizes.json').write_text(json.dumps(
            {'/tmp/wt': {'mtime': 1, 'at': 1, 'bytes': 2 * 1024 ** 3}}))
        # Size alone says nothing about eligibility for removal.
        report = snapshot_mod.reclaim_report()
        self.assertIsNone(report['worktrees_gb'])
        from agentscope_lib import history
        history.append('worktree-summary', {'counts': {'worktrees': 2, 'prune': 1},
                       'reclaimable_bytes_estimate': 2 * 1024 ** 3})
        report = snapshot_mod.reclaim_report()
        self.assertEqual(report['worktrees_gb'], 2.0)
        self.assertEqual(report['removable'], 3)
        self.assertIsNotNone(report['updated_at'])

    def test_05_missing_data_never_crashes(self):
        data = snapshot_mod.build_snapshot(sessions=[], info={}, units=[], alerts=[],
                                           load={'load1': None, 'ncpu': None})
        self.assertEqual(data['sessions'], [])
        self.assertEqual(data['totals']['sessions'], 0)
        self.assertIsNone(data['reclaim']['updated_at'])


class SocketTests(DaemonTestCase):
    def make_server(self, **kwargs):
        path = str(self.root / 'state' / 'agentscope.sock')
        kwargs.setdefault('snapshot_provider', lambda: {'schema': 1, 'sessions': []})
        kwargs.setdefault('clean_targets', lambda: [fake_group()])
        server = server_mod.SnapshotServer(path=path, **kwargs)
        server.start()
        self.addCleanup(server.stop)
        return server

    def test_10_ping_and_snapshot(self):
        server = self.make_server()
        self.assertEqual('0o600', oct(os.stat(server.path).st_mode & 0o777))
        pong = server_mod.request({'op': 'ping'}, path=server.path)
        self.assertTrue(pong['ok'])
        self.assertIn('version', pong)
        snapshot = server_mod.request({'op': 'snapshot'}, path=server.path)
        self.assertTrue(snapshot['ok'])
        self.assertEqual(snapshot['snapshot']['schema'], 1)

    def test_11_clean_dry_run_returns_token_and_targets(self):
        server = self.make_server()
        response = server_mod.request({'op': 'clean', 'dry_run': True}, path=server.path)
        self.assertTrue(response['ok'])
        self.assertTrue(response['token'])
        self.assertEqual(response['targets'][0]['pgid'], 100)
        self.assertEqual(response['targets'][0]['size'], 1)
        self.assertEqual(response['targets'][0]['names'], ['node'])

    def test_12_token_is_single_use(self):
        calls = []
        server = self.make_server(clean_group=fake_cleaner(calls))
        response = server_mod.request({'op': 'clean', 'dry_run': True}, path=server.path)
        token = response['token']
        first = server_mod.request({'op': 'clean', 'dry_run': False, 'token': token},
                                   path=server.path)
        self.assertTrue(first['ok'])
        self.assertEqual(first['cleaned'], [100])
        second = server_mod.request({'op': 'clean', 'dry_run': False, 'token': token},
                                    path=server.path)
        self.assertFalse(second['ok'])
        self.assertIn('token', second['error'])
        self.assertEqual(calls, [100])

    def test_13_token_expires(self):
        server = self.make_server(token_ttl=0.0,
                                  clean_group=lambda group: (True, 'cleaned', None))
        response = server_mod.request({'op': 'clean', 'dry_run': True}, path=server.path)
        expired = server_mod.request({'op': 'clean', 'dry_run': False,
                                      'token': response['token']}, path=server.path)
        self.assertFalse(expired['ok'])
        self.assertIn('token', expired['error'])

    def test_14_only_groups_inside_token_are_cleaned(self):
        calls = []
        server = self.make_server(
            clean_targets=lambda: [fake_group(100), fake_group(200)],
            clean_group=fake_cleaner(calls))
        response = server_mod.request({'op': 'clean', 'dry_run': True}, path=server.path)
        self.assertEqual([t['pgid'] for t in response['targets']], [100, 200])
        # 复核后集合变了 —— token 里的组仍以 token 为准
        server.clean_targets = lambda: [fake_group(300)]
        result = server_mod.request({'op': 'clean', 'dry_run': False,
                                     'token': response['token']}, path=server.path)
        self.assertTrue(result['ok'])
        self.assertEqual(result['cleaned'], [100, 200])
        self.assertEqual(calls, [100, 200])

    def test_14b_group_failing_recheck_is_reported_as_skipped(self):
        server = self.make_server(
            clean_targets=lambda: [fake_group(100)],
            clean_group=lambda group: (False, 'skipped', 'revalidation failed'))
        response = server_mod.request({'op': 'clean', 'dry_run': True}, path=server.path)
        result = server_mod.request({'op': 'clean', 'dry_run': False,
                                     'token': response['token']}, path=server.path)
        self.assertTrue(result['ok'])
        self.assertEqual(result['cleaned'], [])
        self.assertEqual(result['skipped'][0]['pgid'], 100)
        self.assertEqual(result['skipped'][0]['reason'], 'revalidation failed')

    def test_14c_gone_is_separate_from_cleaned(self):
        outcomes = iter([(True, 'cleaned', None), (True, 'gone', None),
                         (False, 'skipped', 'revalidation failed')])
        server = self.make_server(
            clean_targets=lambda: [fake_group(100), fake_group(200), fake_group(300)],
            clean_group=lambda group: next(outcomes))
        response = server.dispatch({'op': 'clean', 'dry_run': True})
        result = server.dispatch({'op': 'clean', 'dry_run': False, 'token': response['token']})
        self.assertEqual(result['cleaned'], [100])
        self.assertEqual(result['gone'], [200])
        self.assertEqual(result['skipped'], [{'pgid': 300, 'reason': 'revalidation failed'}])

    def test_14d_preview_reports_whether_daemon_can_execute(self):
        server = self.make_server()
        for allow_cleanup in (True, False):
            server.allow_cleanup = allow_cleanup
            response = server.dispatch({'op': 'clean', 'dry_run': True})
            self.assertIs(response['executable'], allow_cleanup)

    def test_15_missing_token_is_rejected(self):
        server = self.make_server()
        response = server_mod.request({'op': 'clean', 'dry_run': False}, path=server.path)
        self.assertFalse(response['ok'])
        self.assertIn('token', response['error'])

    def test_16_bad_requests_get_error_response(self):
        server = self.make_server()
        unknown = server_mod.request({'op': 'nope'}, path=server.path)
        self.assertFalse(unknown['ok'])
        self.assertIn('op', unknown['error'])
        import socket as socket_mod
        with socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(server.path)
            client.sendall(b'not json\n')
            data = client.recv(4096)
        payload = json.loads(data.decode().strip())
        self.assertFalse(payload['ok'])
        self.assertIn('json', payload['error'])

    def test_17_server_failure_keeps_service_alive(self):
        server = self.make_server(snapshot_provider=lambda: (_ for _ in ()).throw(RuntimeError('boom')))
        broken = server_mod.request({'op': 'snapshot'}, path=server.path)
        self.assertFalse(broken['ok'])
        self.assertIn('boom', broken['error'])
        alive = server_mod.request({'op': 'ping'}, path=server.path)
        self.assertTrue(alive['ok'])

    def test_existing_server_and_regular_file_are_preserved(self):
        server = self.make_server()
        other = server_mod.SnapshotServer(path=server.path)
        with self.assertRaises(RuntimeError):
            other.start()
        other.stop()
        self.assertTrue(server_mod.ping(path=server.path)['ok'])
        path = str(self.root / 'regular')
        Path(path).write_text('keep')
        with self.assertRaises(RuntimeError):
            server_mod.SnapshotServer(path=path).start()
        self.assertEqual(Path(path).read_text(), 'keep')

    def test_strict_request_framing_and_timeout(self):
        import socket
        server = self.make_server(timeout=0.05)
        for data in (b'{"op":"ping"}', b'x' * server_mod.MAX_REQUEST):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1)
                client.connect(server.path)
                client.sendall(data)
                if len(data) < server_mod.MAX_REQUEST:
                    client.shutdown(socket.SHUT_WR)
                self.assertFalse(json.loads(client.recv(4096))['ok'])
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(server.path)
            self.assertEqual(json.loads(client.recv(4096))['error'], 'timeout')
        self.assertEqual(server_mod.TIMEOUT, 5)
        self.assertEqual(server_mod.TOKEN_TTL, 60)

    def test_connection_has_one_response_and_default_cleanup_has_deadline(self):
        import socket
        server = self.make_server()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(server.path)
            client.sendall(b'{"op":"ping"}\n{"op":"snapshot"}\n')
            response = b''
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response += chunk
        self.assertEqual(response.count(b'\n'), 1)
        with patch.object(cleanup, 'analyze', return_value=({}, [])) as analyze:
            server.clean_targets = server_mod.default_clean_targets
            self.assertTrue(server_mod.request({'op': 'clean', 'dry_run': True}, path=server.path)['ok'])
        self.assertIsInstance(analyze.call_args.kwargs['deadline'], float)

    def test_boolean_required_and_token_snapshot_is_immutable(self):
        calls = []
        group = fake_group()
        server = self.make_server(clean_targets=lambda: [group], clean_group=fake_cleaner(calls))
        token = server.dispatch({'op': 'clean', 'dry_run': True})['token']
        group['pgid'] = 999
        for value in (None, 0, 'false'):
            self.assertFalse(server.dispatch({'op': 'clean', 'dry_run': value, 'token': token})['ok'])
        result = server.dispatch({'op': 'clean', 'dry_run': False, 'token': token, 'pgids': [999]})
        self.assertEqual(result['cleaned'], [100])
        self.assertEqual(calls, [100])

    def test_real_clean_adapter_revalidates_and_audits_socket(self):
        from agentscope_lib import history
        group = fake_group()
        with patch.object(cleanup, 'revalidate_group', return_value=(fake_info(), group)), \
             patch.object(cleanup, 'cleanable', return_value=False), \
             patch.object(cleanup.os, 'kill') as kill:
            ok, result, reason = server_mod.default_clean_group(group)
        self.assertFalse(ok)
        self.assertEqual(reason, 'revalidation failed')
        kill.assert_not_called()
        self.assertEqual(history.read(['kills'])[-1]['mode'], 'socket')


class WatchTests(DaemonTestCase):
    def test_socket_start_failure_does_not_stop_watch(self):
        from types import SimpleNamespace
        with patch.object(server_mod.SnapshotServer, 'start', side_effect=OSError('busy')), \
             patch.object(cleanup, 'watch_round') as round_, \
             patch.object(cleanup.time, 'sleep', side_effect=KeyboardInterrupt):
            self.assertEqual(cleanup.cmd_watch(SimpleNamespace(interval=1, dry_run=True)), 0)
        round_.assert_called_once()

    def test_20_snapshot_failure_does_not_break_round(self):
        args = type('Args', (), {'dry_run': True, 'interval': 30})()
        state = {'daemon': {'pid': os.getpid(), 'version': '1.2.0', 'interval_s': 30,
                            'last_clean_at': None}, 'snapshot': None}
        with patch.object(cleanup, 'analyze', return_value=({}, [])), \
                patch.object(cleanup, 'metrics', create=True), \
                patch('agentscope_lib.metrics.write'), \
                patch('agentscope_lib.worktrees.schedule_weekly_summary'), \
                patch.object(agents, 'collect', return_value=[]), \
                patch.object(snapshot_mod, 'write_snapshot', side_effect=OSError('disk full')):
            result = cleanup.watch_round(args, cleanup.Notifier(), set(), state)
        self.assertIsNone(result['snapshot'])
        self.assertEqual(result['daemon']['pid'], os.getpid())

    def test_21_round_writes_snapshot_file(self):
        args = type('Args', (), {'dry_run': True, 'interval': 30})()
        state = {'daemon': {'pid': os.getpid(), 'version': '1.2.0', 'interval_s': 30,
                            'last_clean_at': None}, 'snapshot': None}
        with patch.object(cleanup, 'analyze', return_value=({}, [])), \
                patch('agentscope_lib.metrics.write'), \
                patch('agentscope_lib.worktrees.schedule_weekly_summary'), \
                patch.object(agents, 'collect', return_value=[]), \
                patch.object(agents, 'alerts', return_value=[]):
            cleanup.watch_round(args, cleanup.Notifier(), set(), state)
        stored = snapshot_mod.read_snapshot()
        self.assertIsNotNone(stored)
        self.assertEqual(stored['daemon']['pid'], os.getpid())
        self.assertEqual(stored['daemon']['interval_s'], 30)
        self.assertEqual('0o600', oct(os.stat(snapshot_mod.snapshot_path()).st_mode & 0o777))


if __name__ == '__main__':
    unittest.main()
