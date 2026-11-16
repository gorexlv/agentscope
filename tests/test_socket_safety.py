"""Socket safety and real-analysis regression; never execute real cleanup."""
import concurrent.futures
import json
import os
from pathlib import Path
import socket
import stat
import threading
import time
import unittest
from unittest.mock import patch

from test_daemon import DaemonTestCase, SocketTests, fake_group, fake_cleaner
from agentscope_lib import cleanup, server as server_mod, snapshot as snapshot_mod


class SocketSafetyTests(DaemonTestCase):
    make_server = SocketTests.make_server

    def test_real_analyze_dry_run_finishes_inside_clean_budget(self):
        # No injected clean_targets, analyze/scan mocks, or prebuilt process fixtures.
        # Read the real snapshot file lazily to avoid an unrelated background scan.
        server = server_mod.SnapshotServer(
            path=str(self.root / 'state' / 'agentscope.sock'),
            snapshot_provider=snapshot_mod.read_snapshot)
        server.start()
        self.addCleanup(server.stop)
        started = time.monotonic()
        response = server_mod.request({'op': 'clean', 'dry_run': True}, path=server.path)
        elapsed = time.monotonic() - started
        self.assertIsNotNone(response)
        self.assertTrue(response['ok'], response)
        self.assertEqual(response['source'], 'live')
        self.assertTrue(response['as_of'])
        self.assertLess(elapsed, server_mod.CLEAN_TIMEOUT)
        print(f'real analyze dry_run: {elapsed:.2f}s, {len(response["targets"])} targets (not executed)')

    def test_cache_freshness_and_live_fallback(self):
        cached = {'groups': [fake_group(101)], 'monotonic': time.monotonic(), 'as_of': 'cached-time'}
        server = self.make_server(clean_cache=lambda: cached, daemon_info={'interval_s': 1})
        with patch.object(cleanup, 'analyze', side_effect=AssertionError('unexpected full scan')):
            response = server.dispatch({'op': 'clean', 'dry_run': True})
        self.assertEqual(response['source'], 'cache')
        self.assertEqual(response['as_of'], 'cached-time')
        self.assertEqual(response['targets'][0]['pgid'], 101)
        cached['monotonic'] -= 3
        response = server.dispatch({'op': 'clean', 'dry_run': True})
        self.assertEqual(response['source'], 'live')
        self.assertEqual(response['targets'][0]['pgid'], 100)

    def test_fresh_cached_preview_bypasses_cleanup_lock_but_live_preview_does_not(self):
        cached = {'groups': [fake_group(101)], 'monotonic': time.monotonic(), 'as_of': 'cached-time'}
        server = self.make_server(clean_cache=lambda: cached, daemon_info={'interval_s': 1})
        with cleanup.CLEANUP_LOCK:
            response = server_mod.request({'op': 'clean', 'dry_run': True}, path=server.path)
            self.assertTrue(response['ok'], response)
            self.assertEqual(response['source'], 'cache')
            cached['monotonic'] -= 3
            response = server_mod.request({'op': 'clean', 'dry_run': True}, path=server.path)
            self.assertEqual(response['error'], 'busy, retry')

    def test_watch_publishes_cache_and_excludes_cleaned_groups(self):
        from types import SimpleNamespace
        from agentscope_lib import agents
        from test_daemon import fake_info
        group = fake_group()
        state = {'daemon': {'interval_s': 30}, 'snapshot': None}
        def cleaned(*args, **kwargs):
            kwargs['outcome']['result'] = 'cleaned'
            return True
        for dry in (True, False):
            with patch.object(cleanup, 'analyze', return_value=(fake_info(), [group])), \
                 patch.object(cleanup, 'cleanable', return_value=True), \
                 patch.object(cleanup, 'kill_group', side_effect=cleaned), \
                 patch('agentscope_lib.metrics.write'), \
                 patch('agentscope_lib.worktrees.schedule_weekly_summary'), \
                 patch.object(agents, 'collect', return_value=[]), \
                 patch.object(cleanup, 'write_daemon_snapshot') as write:
                cleanup.watch_round(SimpleNamespace(dry_run=dry), cleanup.Notifier(), set(), state)
            self.assertEqual(len(state['clean_cache']['groups']), 1 if dry else 0)
            self.assertEqual(len(write.call_args.args[1]), 1 if dry else 0)

    def test_memory_excludes_inactive_file_cache(self):
        from types import SimpleNamespace
        results = [SimpleNamespace(stdout='40960'), SimpleNamespace(stdout=
                   'Mach Virtual Memory Statistics: (page size of 4096 bytes)\n'
                   'Pages active: 2.\nPages wired down: 1.\nPages occupied by compressor: 1.\n'
                   'Pages inactive: 5.\nPages free: 1.\n'), SimpleNamespace(stdout='used = 0M')]
        with patch.object(snapshot_mod.subprocess, 'run', side_effect=results):
            self.assertEqual(snapshot_mod.memory_usage()['mem_used_pct'], 40.0)

    def test_peer_uid_and_unreadable_credentials_fail_closed(self):
        server = self.make_server()
        for kwargs in ({'return_value': os.getuid() + 1}, {'side_effect': OSError('denied')}):
            with patch.object(server_mod, 'peer_uid', **kwargs):
                self.assertIsNone(server_mod.request({'op': 'ping'}, path=server.path))
        self.assertTrue(server_mod.ping(path=server.path)['ok'])

    def test_state_directory_permissions_and_owner(self):
        directory = self.root / 'unsafe'; directory.mkdir(mode=0o700)
        directory.chmod(0o777)
        with self.assertRaises(RuntimeError):
            server_mod.SnapshotServer(path=str(directory / 's')).start()
        directory.chmod(0o700)
        original = os.stat
        def other_owner(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if str(path) == str(directory):
                values = list(result); values[4] = os.getuid() + 1
                return os.stat_result(values)
            return result
        with patch.object(server_mod.os, 'stat', side_effect=other_owner):
            with self.assertRaises(RuntimeError):
                server_mod.SnapshotServer(path=str(directory / 's')).start()

    def test_lease_and_endpoint_symlinks_are_preserved(self):
        target = self.root / 'keep'; target.write_text('keep'); target.chmod(0o644)
        for suffix in ('.lock', ''):
            endpoint = self.root / ('socket' + ('2' if not suffix else '1'))
            Path(str(endpoint) + suffix).symlink_to(target)
            with self.assertRaises((OSError, RuntimeError)):
                server_mod.SnapshotServer(path=str(endpoint)).start()
            self.assertEqual(target.read_text(), 'keep')
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_same_token_concurrently_executes_once(self):
        calls = []
        server = self.make_server(clean_group=fake_cleaner(calls))
        token = server.dispatch({'op': 'clean', 'dry_run': True})['token']
        barrier = threading.Barrier(2)
        def execute():
            barrier.wait()
            return server_mod.request({'op': 'clean', 'dry_run': False, 'token': token}, path=server.path)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: execute(), range(2)))
        self.assertEqual(sum(r['ok'] for r in results), 1)
        self.assertEqual(calls, [100])

    def test_watch_lock_and_distinct_tokens_return_busy_without_consuming(self):
        calls = []
        server = self.make_server(clean_group=fake_cleaner(calls))
        tokens = [server.dispatch({'op': 'clean', 'dry_run': True})['token'] for _ in range(2)]
        # This is the same lock used by watch's analyze and kill_group.
        with cleanup.CLEANUP_LOCK:
            for token in tokens:
                response = server_mod.request({'op': 'clean', 'dry_run': False, 'token': token}, path=server.path)
                self.assertEqual(response['error'], 'busy, retry')
        for token in tokens:
            self.assertTrue(server.dispatch({'op': 'clean', 'dry_run': False, 'token': token})['ok'])
        self.assertEqual(calls, [100, 100])

    def test_dry_run_daemon_refuses_execution(self):
        calls = []
        server = self.make_server(allow_cleanup=False, clean_group=fake_cleaner(calls))
        token = server.dispatch({'op': 'clean', 'dry_run': True})['token']
        result = server.dispatch({'op': 'clean', 'dry_run': False, 'token': token})
        self.assertEqual(result['error'], 'daemon is in dry-run mode')
        self.assertEqual(calls, [])

    def test_connection_limit_and_thread_start_failure_recover(self):
        server = self.make_server()
        for _ in range(server_mod.MAX_CONNECTIONS):
            self.assertTrue(server._connections.acquire(blocking=False))
        try:
            result = server_mod.request({'op': 'ping'}, path=server.path)
            self.assertEqual(result['error'], 'busy, retry')
        finally:
            for _ in range(server_mod.MAX_CONNECTIONS): server._connections.release()
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError('thread limit')):
            self.assertIsNone(server_mod.request({'op': 'ping'}, path=server.path))
        self.assertTrue(server_mod.ping(path=server.path)['ok'])

    def test_timeout_maps_to_busy_and_execution_never_calls_analyze(self):
        server = self.make_server(clean_targets=lambda: (_ for _ in ()).throw(TimeoutError()))
        self.assertEqual(server.dispatch({'op': 'clean', 'dry_run': True})['error'], 'busy, retry')
        server.clean_targets = lambda: [fake_group()]
        token = server.dispatch({'op': 'clean', 'dry_run': True})['token']
        with patch.object(cleanup, 'analyze', side_effect=AssertionError('full analyze forbidden')), \
             patch.object(cleanup, 'revalidate_group', return_value=({}, None)), \
             patch.object(cleanup, 'parent_exited', return_value=True), \
             patch.object(cleanup.os, 'kill') as kill:
            result = server.dispatch({'op': 'clean', 'dry_run': False, 'token': token})
        self.assertEqual(result['cleaned'], [])
        self.assertEqual(result['gone'], [100])
        kill.assert_not_called()


# Avoid rediscovering the imported base test suite in this module.
del SocketTests
