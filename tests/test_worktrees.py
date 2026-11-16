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

    def test_weekly_collection_failure_is_throttled(self):
        with patch.object(w, 'collect', side_effect=OSError('scan failed')) as collect:
            with self.assertRaises(OSError):
                w.weekly_summary()
            self.assertIsNone(w.weekly_summary())
        self.assertEqual(collect.call_count, 1)

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

    def ignore(self, pattern):
        with (self.repo/'.git/info/exclude').open('a') as f:
            f.write(pattern+'\n')

    def test_ignored_private_files_and_revalidation(self):
        self.ignore('.env*')
        before = self.report()
        (self.link/'.env.local').write_text('private local settings')
        row = self.row()
        self.assertEqual(row['category'], 'manual')
        self.assertIn('ignored_files', row['reasons'])
        self.assertEqual(row['ignored_paths'], ['.env.local'])
        result = w.clean(before, yes=True, include={'worktrees'})
        self.assertEqual(result['outcomes'][0]['result'], 'skipped')
        self.assertTrue((self.link/'.env.local').exists())

    def test_ignored_artifact_needs_marker(self):
        target = self.link/'node_modules'
        target.mkdir()
        (target/'generated').write_text('dependency')
        self.assertEqual(self.row()['category'], 'worktrees')
        (self.link/'package.json').unlink()
        self.assertIn('ignored_files', self.row()['reasons'])

    def test_ignored_marked_artifact_can_be_removed(self):
        target = self.link/'node_modules'
        target.mkdir()
        (target/'generated').write_text('dependency')
        result = w.clean(self.report(), yes=True, include={'worktrees'})
        self.assertEqual(result['outcomes'][0]['result'], 'cleaned')
        self.assertFalse(self.link.exists())

    def test_artifact_containing_nested_repository_is_refused(self):
        target = self.repo/'node_modules'
        target.mkdir()
        inner = target/'local'
        inner.mkdir()
        self.g('init', '-b', 'main', repo=inner)
        self.assertFalse(w.artifact_safe(str(target), str(self.repo), config.load_config(), time.monotonic()+5))

    def test_ignored_paths_sample_is_bounded(self):
        self.ignore('*.local')
        for i in range(8):
            (self.link/(str(i)+'.local')).write_text('local')
        row = self.row()
        self.assertEqual(row['ignored_count'], 8)
        self.assertEqual(len(row['ignored_paths']), 5)

    def test_slim_nested_worktree_and_repository_boundaries(self):
        self.ignore('wt/')
        self.ignore('nested/')
        nested = self.repo/'wt'
        self.g('worktree', 'add', '-b', 'nested', str(nested))
        (nested/'build').mkdir()
        important = nested/'build/keep.js'
        important.write_text('tracked')
        self.g('add', 'build', repo=nested)
        self.g('commit', '-m', 'keep nested source', repo=nested)
        important.write_text('modified local source')
        other = self.repo/'nested'
        other.mkdir()
        self.g('init', '-b', 'main', repo=other)
        (other/'build').mkdir()
        (other/'package.json').write_text('{}')
        (other/'build/local.js').write_text('local')
        self.assertEqual(list(w.artifacts(str(self.repo), time.monotonic()+5)), [])
        cfg = config.load_config()
        self.assertFalse(w.artifact_safe(str(nested/'build'), str(self.repo), cfg, time.monotonic()+5))
        self.assertFalse(w.artifact_safe(str(nested/'build'), str(nested), cfg, time.monotonic()+5))
        self.assertFalse(w.artifact_safe(str(other/'build'), str(other), cfg, time.monotonic()+5))
        # Force the parent classification only to exercise artifact enumeration independently of B1.
        real_inspect = w.inspect
        def parent_slim(*args, **kwargs):
            row = real_inspect(*args, **kwargs)
            if row['path'] == str(self.repo):
                row['category'] = 'slim'
            return row
        with patch.object(w, 'inspect', side_effect=parent_slim):
            data = self.report()
        self.assertEqual(data['artifacts'], [])
        self.assertEqual(important.read_text(), 'modified local source')

    def test_detached_unmerged_is_never_idle_eligible(self):
        self.g('checkout', '--detach', repo=self.link)
        self.assertEqual(self.row()['category'], 'worktrees')
        (self.link/'file').write_text('detached change')
        self.g('commit', '-am', 'detached', repo=self.link)
        self.g('update-ref', 'refs/remotes/origin/old', 'HEAD', repo=self.link)
        data = w.collect(budget=10, days=0)
        row = next(r for r in data['items'] if r['path'] == str(self.link))
        self.assertEqual(row['unpushed'], 0)
        self.assertIn('detached_unmerged', row['reasons'])
        self.assertEqual(row['category'], 'manual')

    def test_default_ref_missing_and_branch_reasons(self):
        data = self.report()
        branches = {r['branch']: r for r in data['branches']}
        self.assertIn('default_branch', branches['main']['reasons'])
        self.assertIn('checked_out', branches['topic']['reasons'])
        self.g('symbolic-ref', '--delete', 'refs/remotes/origin/HEAD')
        row = self.row()
        self.assertIsNone(row['merged'])
        self.assertEqual(row['category'], 'manual')
        data = w.collect(budget=10, days=0)
        self.assertEqual(next(r for r in data['items'] if r['path'] == str(self.link))['category'], 'worktrees')
        self.assertTrue(all(r['category'] == 'manual' for r in data['branches']))
        self.g('checkout', '--detach', repo=self.link)
        self.assertIn('detached_unmerged', self.row()['reasons'])

    def test_git_failure_unknown_and_path_unknown(self):
        real_git = w.git
        def fail_status(repo, *args, **kwargs):
            if args[0] == 'status':
                raise ValueError('status unavailable')
            return real_git(repo, *args, **kwargs)
        with patch.object(w, 'git', side_effect=fail_status):
            data = self.report()
        self.assertEqual(data['summary']['counts']['unknown'], 2)
        self.assertFalse(data['complete'])
        self.assertTrue(all('query_failed' in r['reasons'] for r in data['items']))
        entries = w.registrations(self.repo)
        entry = next(e for e in entries if e['worktree'] == str(self.link))
        with patch.object(w.os.path, 'isdir', return_value=False), patch.object(w.os.path, 'lexists', return_value=False), patch.object(w.os, 'lstat', side_effect=PermissionError('unreadable')):
            row = w.inspect(str(self.repo), entry, str(self.repo), config.load_config(), [], True, time.monotonic()+5)
        self.assertIn('path_unknown', row['reasons'])
        self.assertEqual(row['category'], 'manual')

    def test_submodule_is_manual(self):
        sub = self.root/'submodule'
        subprocess.run(['git', 'clone', str(self.repo), str(sub)], check=True, capture_output=True)
        self.g('-c', 'protocol.file.allow=always', 'submodule', 'add', str(sub), 'vendor', repo=self.link)
        self.g('commit', '-am', 'add submodule', repo=self.link)
        self.g('update-ref', 'refs/remotes/origin/main', 'HEAD', repo=self.link)
        row = self.row()
        self.assertIn('submodules', row['reasons'])
        self.assertEqual(row['category'], 'manual')

    def test_weekly_failed_audit_throttles_and_marks_partial(self):
        from unittest.mock import Mock
        cfg = config.load_config()
        cfg['notify']['enabled'] = True
        data = self.report()
        data['complete'] = False
        notifier = Mock()
        with patch.object(w, 'collect', return_value=data) as collect, patch.object(w.history, 'append', return_value=False):
            record = w.weekly_summary(notifier, cfg)
            self.assertIsNone(w.weekly_summary(notifier, cfg))
        collect.assert_called_once_with(cfg=cfg, budget=w.WEEKLY_BUDGET)
        self.assertFalse(record['complete'])
        self.assertIn('unknown', record['counts'])
        self.assertIn('部分盘点', notifier.send.call_args.args[0][0]['msg'])

    def test_slim_preserves_env_and_removes_venv(self):
        from agentscope_lib import cleanup
        self.cfgpath.write_text(json.dumps({'workspaces': [str(self.root)], 'worktrees': {'stale_days': 0}}))
        (self.repo/'pyproject.toml').write_text('[project]\nname="test"')
        self.g('add', 'pyproject.toml')
        self.g('commit', '-m', 'python marker')
        self.g('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.ignore('.env')
        self.ignore('.venv/')
        (self.repo/'.env').write_text('private')
        (self.repo/'.venv').mkdir()
        (self.repo/'.venv/generated').write_text('generated')
        data = self.report()
        parent = next(r for r in data['items'] if r['path'] == str(self.repo))
        self.assertIn('ignored_files', parent['reasons'])
        self.assertEqual(parent['category'], 'slim')
        with patch.object(cleanup, 'analyze', return_value=({}, [])):
            result = w.clean(data, yes=True, include={'slim'})
        self.assertEqual(result['outcomes'][0]['result'], 'cleaned', result)
        self.assertEqual((self.repo/'.env').read_text(), 'private')
        self.assertFalse((self.repo/'.venv').exists())

    def test_artifact_links_unlink_only_and_do_not_measure_target(self):
        from agentscope_lib import cleanup, disk
        self.cfgpath.write_text(json.dumps({'workspaces': [str(self.root)], 'worktrees': {'stale_days': 0}}))
        target = self.root/'shared'
        target.mkdir()
        (target/'keep').write_text('must survive')
        (target/'.git').mkdir()
        link = self.repo/'node_modules'
        link.symlink_to(target, target_is_directory=True)
        self.assertFalse(w.artifact_safe(str(link), str(self.repo), config.load_config(), time.monotonic()+5))
        self.assertFalse(disk.clean(str(link), yes=True))
        self.assertEqual((target/'keep').read_text(), 'must survive')
        self.ignore('node_modules')
        self.assertTrue(w.artifact_safe(str(link), str(self.repo), config.load_config(), time.monotonic()+5))
        with patch.object(disk.os, 'walk', side_effect=AssertionError('must not traverse link')):
            self.assertEqual(w.SizeCache().measure(str(link), time.monotonic()+5)[0], os.lstat(link).st_blocks*512)
            self.assertEqual(disk.size_of(str(link), time.monotonic()+5)[0], os.lstat(link).st_blocks*512)
        data = self.report()
        self.assertTrue(any(r['path'] == str(link) for r in data['artifacts']))
        with patch.object(cleanup, 'analyze', return_value=({}, [])):
            result = w.clean(data, yes=True, include={'slim'})
        self.assertEqual(result['outcomes'][0]['result'], 'cleaned', result)
        self.assertFalse(link.is_symlink())
        self.assertEqual((target/'keep').read_text(), 'must survive')
        self.assertTrue((target/'.git').is_dir())
        (self.link/'node_modules').symlink_to(target, target_is_directory=True)
        result = w.clean(self.report(), yes=True, include={'worktrees'})
        self.assertTrue(any(r['result'] == 'cleaned' for r in result['outcomes']))
        self.assertEqual((target/'keep').read_text(), 'must survive')

    def test_cache_whitelist_requires_project_marker(self):
        from agentscope_lib import disk, cleanup
        self.ignore('__pycache__/')
        cache = self.link/'__pycache__'
        cache.mkdir()
        (cache/'generated.pyc').write_bytes(b'cache')
        self.assertIn('ignored_files', self.row()['reasons'])
        (self.link/'pyproject.toml').write_text('[project]\nname="test"')
        self.g('add', 'pyproject.toml', repo=self.link)
        self.g('commit', '-m', 'python marker', repo=self.link)
        self.g('update-ref', 'refs/remotes/origin/main', 'HEAD', repo=self.link)
        self.assertEqual(self.row()['category'], 'worktrees')
        with patch.object(cleanup, 'analyze', return_value=({}, [])):
            self.assertTrue(disk.clean(str(cache), yes=True))
        self.ignore('arbitrary-cache/')
        (self.link/'arbitrary-cache').mkdir()
        (self.link/'arbitrary-cache/CACHEDIR.TAG').write_text('cache tag')
        self.assertIn('ignored_files', self.row()['reasons'])

    def test_branch_inventory_batches_ancestry(self):
        for index in range(8):
            self.g('branch', 'merged-'+str(index))
        real_git, calls = w.git, []
        def record(repo, *args, **kwargs):
            calls.append(args)
            return real_git(repo, *args, **kwargs)
        with patch.object(w, 'git', side_effect=record), patch.object(w, 'ancestor', side_effect=AssertionError('not per branch')):
            rows = w.branch_rows(str(self.repo), w.registrations(self.repo), config.load_config(), time.monotonic()+5)
        self.assertEqual(sum(r['category'] == 'branches' for r in rows), 8)
        self.assertEqual(sum(any(a.startswith('--merged=') for a in c) for c in calls), 1)
        self.assertFalse(any(c[0] == 'rev-list' for c in calls))

    def test_rotation_persists_and_prefers_oldest(self):
        pairs = [('recent', []), ('old', []), ('never', [])]
        w.save_checks({'recent': 20, 'old': 10})
        ordered, stamps = w.repository_order(pairs)
        self.assertEqual([r for r, _ in ordered], ['never', 'old', 'recent'])
        stamps['never'] = 30
        w.save_checks(stamps)
        self.assertEqual([r for r, _ in w.repository_order(pairs)[0]], ['old', 'recent', 'never'])

    def test_repository_evidence_runs_concurrently_before_sizes(self):
        import threading
        pairs = [('one', [{'worktree': str(self.repo)}]), ('two', [{'worktree': str(self.link)}])]
        barrier = threading.Barrier(2)
        completed = []
        def inspect(repo, *args, **kwargs):
            barrier.wait(timeout=3)
            completed.append(repo)
            return {'path': repo, 'exists': False, 'category': 'manual', 'reasons': [], 'bytes': None}
        with patch.object(w, 'discover', return_value=(pairs, [])), patch.object(w, 'inspect', side_effect=inspect), patch.object(w, 'branch_rows', return_value=[]):
            data = w.collect(budget=4)
        self.assertEqual(set(completed), {'one', 'two'})
        self.assertEqual(len(data['items']), 2)

    def test_cli_budget_is_forwarded(self):
        import argparse
        from agentscope_lib import extensions
        parser = argparse.ArgumentParser()
        extensions.register(parser.add_subparsers())
        args = parser.parse_args(['worktrees', '--budget', '17'])
        with patch.object(w, 'collect', return_value={}) as collect, patch.object(extensions, 'output'):
            args.func(args)
        self.assertEqual(collect.call_args.kwargs['budget'], 17)

    def test_weekly_background_returns_without_waiting_and_deduplicates(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        def slow(*args, **kwargs):
            entered.set()
            release.wait(timeout=3)
        with patch.object(w, 'weekly_summary', side_effect=slow):
            try:
                start = time.monotonic()
                self.assertTrue(w.schedule_weekly_summary(cfg=config.load_config()))
                self.assertLess(time.monotonic()-start, .5)
                self.assertTrue(entered.wait(timeout=1))
                self.assertFalse(w.schedule_weekly_summary())
                with w._weekly_lock:
                    worker = w._weekly_workers[w.history.state_dir()]
            finally:
                release.set()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())

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
