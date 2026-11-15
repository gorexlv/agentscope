import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from agentscope_lib import config, worktrees as w


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root/'main'
        self.repo.mkdir()
        self.cfgpath = self.root/'config.json'
        self.cfgpath.write_text(json.dumps({'workspaces': [str(self.root)]}))
        env = patch.dict(os.environ, AGENTSCOPE_CONFIG=str(self.cfgpath), AGENTSCOPE_WORKSPACES='',
                         AGENTSCOPE_STATE_DIR=str(self.root/'state'))
        env.start()
        self.addCleanup(env.stop)
        self.g('init', '-b', 'main')
        self.g('config', 'user.email', 'test@example.invalid')
        self.g('config', 'user.name', 'Test')
        (self.repo/'file').write_text('base')
        (self.repo/'.gitignore').write_text('node_modules/\n')
        (self.repo/'package.json').write_text('{}')
        self.g('add', '.')
        self.g('commit', '-m', 'base')
        self.g('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.g('symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main')
        self.link = self.root/'linked space'
        self.g('worktree', 'add', '-b', 'topic', str(self.link))
        proc = patch.object(w, 'process_snapshot', return_value=([], True))
        proc.start()
        self.addCleanup(proc.stop)

    def g(self, *args, repo=None):
        return w.git(repo or self.repo, *args)

    def report(self):
        return w.collect(budget=10)

    def row(self):
        return next(r for r in self.report()['items'] if r['path'] == str(self.link))

    def test_eligible_main_and_dry_run(self):
        data = self.report()
        self.assertTrue(data['complete'], data)
        self.assertEqual(self.row()['category'], 'worktrees')
        self.assertIn('main_worktree', data['items'][0]['reasons'])
        result = w.clean(data)
        self.assertTrue(result['dry_run'])
        self.assertTrue(self.link.exists())
        result = w.clean(data, yes=True, include={'worktrees'})
        self.assertEqual(result['outcomes'][0]['result'], 'cleaned', result)
        self.assertFalse(self.link.exists())

    def test_dirty_and_untracked(self):
        (self.link/'file').write_text('dirty')
        self.assertIn('dirty_or_untracked', self.row()['reasons'])
        self.g('restore', 'file', repo=self.link)
        (self.link/'new').write_text('untracked')
        self.assertIn('dirty_or_untracked', self.row()['reasons'])

    def test_unpushed(self):
        (self.link/'file').write_text('new commit')
        self.g('commit', '-am', 'unpublished', repo=self.link)
        row = self.row()
        self.assertEqual(row['unpushed'], 1)
        self.assertIn('unpushed', row['reasons'])
        self.assertEqual(row['category'], 'manual')

    def test_in_use_and_unknown(self):
        with patch.object(w, 'process_snapshot', return_value=([str(self.link/'sub')], True)):
            self.assertIn('process_in_use', self.row()['reasons'])
        with patch.object(w, 'process_snapshot', return_value=([], False)):
            self.assertIn('process_cwds_unknown', self.row()['reasons'])

    def test_locked(self):
        self.g('worktree', 'lock', str(self.link))
        self.assertIn('locked', self.row()['reasons'])

    def test_protected(self):
        self.cfgpath.write_text(json.dumps({'workspaces': [str(self.root)], 'worktrees': {'protect': [str(self.link)]}}))
        self.assertIn('protected', self.row()['reasons'])

    def test_orca_unavailable_and_routing(self):
        with patch.object(w, 'tool_for', return_value='orca'), patch.object(w.shutil, 'which', return_value=None):
            self.assertIn('orca_unavailable', self.row()['reasons'])
        data = self.report()
        real_run = w.run
        called = []
        def run(argv, deadline=None):
            if argv[0] == 'orca':
                called.append(argv)
                return '{}'
            return real_run(argv, deadline)
        with patch.object(w, 'tool_for', return_value='orca'), patch.object(w.shutil, 'which', return_value='/bin/orca'), patch.object(w, 'run', side_effect=run):
            w.clean(data, yes=True, include={'worktrees'})
        self.assertEqual(called, [['orca', 'worktree', 'rm', '--worktree', 'path:'+str(self.link), '--json']])

    def test_prune_missing_and_locked_guard(self):
        shutil.rmtree(self.link)
        self.assertEqual(self.row()['category'], 'prune')
        data = self.report()
        result = w.clean(data, yes=True, include={'prune'})
        self.assertEqual(result['outcomes'][0]['result'], 'cleaned', result)
        self.assertEqual(len(w.registrations(self.repo)), 1)

    def test_prune_checks_other_missing_entries(self):
        other = self.root/'locked'
        self.g('worktree', 'add', '-b', 'locked', str(other))
        self.g('worktree', 'lock', str(other))
        shutil.rmtree(other)
        shutil.rmtree(self.link)
        result = w.clean(self.report(), yes=True, include={'prune'})
        self.assertEqual(result['outcomes'][0]['result'], 'skipped')
        self.assertEqual(len(w.registrations(self.repo)), 3)

    def test_branches_merged_and_unmerged(self):
        self.g('branch', 'merged')
        self.g('branch', 'unmerged')
        self.g('checkout', 'unmerged', repo=self.link)
        (self.link/'file').write_text('diverged')
        self.g('commit', '-am', 'diverged', repo=self.link)
        self.g('update-ref', 'refs/remotes/origin/unmerged', 'HEAD', repo=self.link)
        self.g('checkout', 'topic', repo=self.link)
        data = self.report()
        rows = {r['branch']: r for r in data['branches']}
        self.assertEqual(rows['merged']['category'], 'branches')
        self.assertIn('not_merged', rows['unmerged']['reasons'])
        result = w.clean(data, yes=True, include={'branches'})
        self.assertTrue(all(r['result'] == 'cleaned' for r in result['outcomes']), result)
        self.assertNotIn('refs/heads/merged', self.g('for-each-ref', '--format=%(refname)', 'refs/heads'))
        self.assertIn('refs/heads/unmerged', self.g('for-each-ref', '--format=%(refname)', 'refs/heads'))

    def test_revalidation_dirty_head_and_protection(self):
        data = self.report()
        (self.link/'new').write_text('new')
        result = w.clean(data, yes=True, include={'worktrees'})
        self.assertEqual(result['outcomes'][0]['result'], 'skipped')
        self.assertTrue(self.link.exists())
        (self.link/'new').unlink()
        self.cfgpath.write_text(json.dumps({'workspaces': [str(self.root)], 'worktrees': {'protect': [str(self.link)]}}))
        self.assertEqual(w.clean(data, yes=True, include={'worktrees'})['outcomes'][0]['result'], 'skipped')

    def test_slim_stale_main(self):
        self.cfgpath.write_text(json.dumps({'workspaces': [str(self.root)], 'worktrees': {'stale_days': 0}}))
        artifact = self.repo/'node_modules'
        artifact.mkdir()
        (artifact/'generated').write_text('artifact')
        data = self.report()
        self.assertEqual(data['items'][0]['category'], 'slim', data)
        self.assertEqual(len(data['artifacts']), 1)
        from agentscope_lib import cleanup
        # Real artifact deletion with only process evidence substituted.
        with patch.object(cleanup, 'analyze', return_value=({}, [])):
            result = w.clean(data, yes=True, include={'slim'})
        self.assertEqual(result['outcomes'][0]['result'], 'cleaned', result)
        self.assertFalse(artifact.exists())
        self.assertTrue(self.repo.exists())

    def test_slim_refuses_tracked_artifacts(self):
        self.cfgpath.write_text(json.dumps({'workspaces': [str(self.root)], 'worktrees': {'stale_days': 0}}))
        artifact = self.repo/'node_modules'
        artifact.mkdir()
        (artifact/'important').write_text('tracked')
        self.g('add', '-f', 'node_modules/important')
        self.g('commit', '-m', 'tracked artifact')
        self.g('update-ref', 'refs/remotes/origin/main', 'HEAD')
        data = self.report()
        self.assertEqual(data['items'][0]['category'], 'slim')
        self.assertEqual(data['artifacts'], [])

    def test_branch_revalidation_and_symlink(self):
        self.g('branch', 'old')
        data = self.report()
        self.g('checkout', 'old', repo=self.link)
        result = w.clean(data, yes=True, include={'branches'})
        self.assertEqual(result['outcomes'][0]['result'], 'skipped')
        real = self.root/'moved'
        self.link.rename(real)
        self.link.symlink_to(real, target_is_directory=True)
        self.assertIn('symlink_path', self.row()['reasons'])

    def test_idle_rule_and_online_pr_identity(self):
        (self.link/'file').write_text('pushed')
        self.g('commit', '-am', 'pushed', repo=self.link)
        self.g('update-ref', 'refs/remotes/origin/topic', 'HEAD', repo=self.link)
        self.assertIn('not_merged_or_idle', self.row()['reasons'])
        self.cfgpath.write_text(json.dumps({'workspaces': [str(self.root)], 'worktrees': {'idle_days': 0}}))
        self.assertEqual(self.row()['category'], 'worktrees')
        self.g('remote', 'add', 'origin', 'https://github.com/example/repo.git')
        real_run = w.run
        def mock_run(argv, deadline=None):
            if argv[0] == 'gh':
                return json.dumps([{'state': 'MERGED', 'headRefOid': 'wrong'}])
            return real_run(argv, deadline)
        with patch.object(w, 'run', side_effect=mock_run):
            self.assertIsNone(w.pr_state(self.repo, 'topic', self.g('rev-parse', 'HEAD', repo=self.link).strip(), None))

    def test_cache_and_budget(self):
        first = self.report()
        second = self.report()
        self.assertTrue(all(r.get('size_cached') for r in second['items']))
        self.assertFalse(w.collect(budget=0)['complete'])
        self.assertGreaterEqual(first['summary']['reclaimable_bytes_estimate'], 0)

    def test_weekly_and_invalid_config(self):
        self.assertIsNotNone(w.weekly_summary())
        with patch.object(w, 'collect', side_effect=AssertionError('should not rescan')):
            self.assertIsNone(w.weekly_summary())
        self.cfgpath.write_text(json.dumps({'worktrees': {'idle_days': -1}}))
        self.assertFalse(config.load_config()['valid'])

    def test_timeout_retains_registered_paths(self):
        with patch.object(w, 'activity', side_effect=TimeoutError('slow tree')):
            data = self.report()
        self.assertEqual(len(data['items']), 2)
        self.assertFalse(data['complete'])
        self.assertTrue(all('query_failed' in r['reasons'] for r in data['items']))

    def test_protected_descendant_blocks_parent(self):
        self.cfgpath.write_text(json.dumps({'workspaces': [str(self.root)], 'worktrees': {'protect': [str(self.link/'keep')]}}))
        self.assertIn('protected', self.row()['reasons'])

    def test_online_merged_closed_and_network_opt_in(self):
        (self.link/'file').write_text('pushed')
        self.g('commit', '-am', 'pushed', repo=self.link)
        self.g('update-ref', 'refs/remotes/origin/topic', 'HEAD', repo=self.link)
        for state in ('MERGED', 'CLOSED'):
            with patch.object(w, 'pr_state', return_value=state):
                data = w.collect(budget=10, online=True)
            row = next(r for r in data['items'] if r['path'] == str(self.link))
            self.assertEqual(row['category'], 'worktrees')
        # Explicit fetch is exercised only against a temporary local bare remote.
        remote = self.root/'remote.git'
        subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
        self.g('remote', 'add', 'origin', str(remote))
        self.g('push', 'origin', 'main', 'topic')
        self.assertTrue(w.collect(budget=10, fetch=True)['complete'])

    def test_weekly_notification_and_audit_failure(self):
        from unittest.mock import Mock
        notifier = Mock()
        cfg = config.load_config()
        cfg['notify']['enabled'] = True
        with patch.object(w.history, 'append', return_value=False):
            result = w.weekly_summary(notifier, cfg)
        self.assertIsNotNone(result)
        self.assertEqual(notifier.send.call_args.args[0][0]['kind'], 'WORKTREE_SUMMARY')

    def test_remote_url_strips_credentials(self):
        self.assertEqual(w.remote_slug('https://user:secret@github.com/owner/repo.git'), 'github.com/owner/repo')
        self.assertEqual(w.remote_slug('git@github.com:owner/repo.git'), 'github.com/owner/repo')

    def test_replaced_repository_rejected(self):
        before = self.report()
        shutil.rmtree(self.link)
        self.link.mkdir()
        self.g('init', '-b', 'topic', repo=self.link)
        self.g('fetch', str(self.repo), 'main', repo=self.link)
        self.g('reset', '--hard', 'FETCH_HEAD', repo=self.link)
        row = self.row()
        self.assertEqual(row['category'], 'manual')
        result = w.clean(before, yes=True, include={'worktrees'})
        self.assertEqual(result['outcomes'][0]['result'], 'skipped')
        self.assertTrue(self.link.exists())

    def test_no_implicit_network(self):
        real_run = w.run
        calls = []
        def record(argv, deadline=None):
            calls.append(argv)
            return real_run(argv, deadline)
        with patch.object(w, 'run', side_effect=record):
            self.report()
        self.assertFalse(any('fetch' in c or c[0] == 'gh' for c in calls))
        self.assertTrue(all('--no-optional-locks' in c for c in calls if c[0] == 'git'))


if __name__ == '__main__':
    unittest.main()
