"""Bounded inventory and explicit, revalidated Git workspace maintenance."""
import datetime
import json
import os
import re
from urllib.parse import urlsplit
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from . import disk, history
from .config import load_config, protected, within
from .lifecycle import remaining

KINDS = {'worktrees', 'prune', 'branches', 'slim'}
UNKNOWN_REASONS = {'query_failed', 'process_cwds_unknown', 'path_unknown'}
WEEKLY_BUDGET = 5
_weekly_attempts = {}
SCAN_SKIP = disk.ARTIFACTS | {'.git', '.cache', '.vercel', '__pycache__'}


def run(argv, deadline=None):
    result = subprocess.run(argv, capture_output=True, text=True,
                            timeout=remaining(deadline, 10))
    if result.returncode:
        raise ValueError(result.stderr.strip() or 'command failed')
    return result.stdout


def git(repo, *args, deadline=None):
    return run(['git', '--no-optional-locks', '-C', str(repo), *args], deadline)


def registrations(repo, deadline=None):
    # -z avoids quoted/escaped paths (including spaces and newlines).
    raw = git(repo, 'worktree', 'list', '--porcelain', '-z', deadline=deadline)
    rows, row = [], {}
    for field in raw.split('\0'):
        if not field:
            if row:
                rows.append(row)
                row = {}
            continue
        key, _, value = field.partition(' ')
        row[key] = value if value else True
    if row:
        rows.append(row)
    return rows


def discover(roots, deadline):
    repos, errors, seen = [], [], set()
    for base in roots:
        if not os.path.isdir(base):
            continue
        for path, dirs, files in os.walk(base, followlinks=False, onerror=lambda e: errors.append(str(e))):
            if time.monotonic() >= deadline:
                errors.append('discovery budget exhausted')
                return repos, errors
            dirs[:] = [d for d in dirs if d not in SCAN_SKIP
                       and not os.path.islink(os.path.join(path, d))]
            if '.git' not in files and not (Path(path)/'.git').is_dir():
                continue
            try:
                rows = registrations(path, deadline)
                if not rows:
                    continue
                main = rows[0]['worktree']
                # A linked worktree can discover a main repository outside configured roots.
                if main not in seen:
                    seen.add(main)
                    repos.append((path, rows))
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                errors.append({'path': path, 'error': str(error)})
    return repos, errors


def process_snapshot(deadline=None):
    from . import procstat
    info = procstat.scan_processes(deadline=deadline)
    pids = [p for p, b in info.items() if b.get('uid') == os.getuid()]
    cwds = procstat.load_cwds(pids, deadline=deadline)
    unknown = []
    for pid in pids:
        if cwds.get(pid):
            continue
        # Short-lived processes can exit between enumeration and lsof.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except OSError:
            pass
        unknown.append(pid)
    return list(cwds.values()), bool(pids) and not unknown


def tool_for(path):
    parts = Path(path).parts
    if 'orca' in parts and 'workspaces' in parts:
        return 'orca'
    if '.claude' in parts:
        return 'claude'
    if '.codex' in parts:
        return 'codex'
    if any(p.startswith('multica_workspaces') for p in parts):
        return 'multica'
    return 'manual'


def path_protected(path, cfg):
    return (within(path, cfg['worktrees']['protect']) or
            any(within(p, [os.path.realpath(path)]) for p in cfg['worktrees']['protect']) or
            protected({'comm': '', 'name': '', 'cmdline': ''}, path, cfg))


def default_ref(repo, deadline):
    try:
        return git(repo, 'symbolic-ref', 'refs/remotes/origin/HEAD', deadline=deadline).strip()
    except ValueError:
        # Do not guess which branch is the remote default.
        return None


def ancestor(repo, head, target, deadline):
    if not target:
        return None
    result = subprocess.run(['git', '--no-optional-locks', '-C', str(repo),
                             'merge-base', '--is-ancestor', head, target],
                            capture_output=True, timeout=remaining(deadline, 10))
    if result.returncode not in (0, 1):
        raise ValueError('cannot establish ancestry')
    return result.returncode == 0


def remote_slug(remote):
    if '://' in remote:
        parsed = urlsplit(remote)
        host, path = parsed.hostname, parsed.path.lstrip('/')
    else:
        match = re.fullmatch(r'(?:[^@/]+@)?([^:/]+):(.+)', remote)
        if not match:
            raise ValueError('PR lookup requires a hosted origin URL')
        host, path = match.groups()
    path = path.removesuffix('.git')
    if not host or not re.fullmatch(r'[^/]+/[^/]+', path):
        raise ValueError('PR lookup requires owner/repository')
    return host + '/' + path


def pr_state(repo, branch, head, deadline):
    if not branch:
        return None
    data = json.loads(run(['gh', 'pr', 'list', '--state', 'all', '--head', branch,
                          '--json', 'state,headRefOid', '--limit', '100', '--repo',
                          remote_slug(git(repo, 'remote', 'get-url', 'origin', deadline=deadline).strip())], deadline))
    # A PR for an older incarnation of the same branch is not evidence for HEAD.
    states = [r['state'] for r in data if r.get('headRefOid') == head]
    return next((s for s in states if s in ('MERGED', 'CLOSED')), states[0] if states else None)


def activity(path, deadline):
    """Latest filesystem activity, excluding regenerable artifacts and Git objects."""
    latest = os.stat(path).st_mtime
    errors = []
    for root, dirs, files in os.walk(path, followlinks=False, onerror=errors.append):
        remaining(deadline, 1)
        dirs[:] = [d for d in dirs if d not in SCAN_SKIP
                   and not os.path.islink(os.path.join(root, d))]
        for name in dirs + files:
            remaining(deadline, 1)
            latest = max(latest, os.lstat(os.path.join(root, name)).st_mtime)
    if errors:
        raise OSError('activity scan incomplete')
    return latest


def ignored_files(worktree, cfg, deadline):
    """Only marked regenerable directories may hide untracked content."""
    raw = git(worktree, 'ls-files', '-z', '-o', '-i', '--exclude-standard',
              '--directory', deadline=deadline)
    rejected, count = [], 0
    for relative in filter(None, raw.split('\0')):
        remaining(deadline, 1)
        parts = Path(relative).parts
        allowed = False
        for index, name in enumerate(parts):
            if name not in disk.ARTIFACTS:
                continue
            candidate = os.path.join(worktree, *parts[:index+1])
            if (os.path.isdir(candidate) and not os.path.islink(candidate) and
                    not os.path.lexists(os.path.join(candidate, '.git')) and
                    disk.deletion_evidence(candidate, cfg['workspaces']) == worktree):
                allowed = True
                break
        if not allowed:
            count += 1
            if len(rejected) < 5:
                rejected.append(relative)
    return rejected, count


def inspect(repo, entry, main, cfg, cwds, cwd_complete, deadline, days=None, online=False):
    path = entry['worktree']
    row = {'path': path, 'repo': repo, 'head': entry.get('HEAD'),
           'branch': str(entry.get('branch', '')).removeprefix('refs/heads/'),
           'tool': tool_for(path), 'main': path == main, 'locked': 'locked' in entry,
           'exists': os.path.isdir(path), 'dirty': None, 'unpushed': None,
           'merged': None, 'pr_state': None, 'last_activity': None, 'idle_days': None,
           'bytes': None, 'size_complete': False, 'reasons': [], 'category': 'manual',
           'ignored_paths': [], 'ignored_count': 0}
    reasons = row['reasons']
    if not cfg['valid']:
        reasons.append('invalid_config')
    if row['main']:
        reasons.append('main_worktree')
    if row['locked']:
        reasons.append('locked')
    if path_protected(path, cfg):
        reasons.append('protected')
    if os.path.lexists(path) and os.path.realpath(path) != path:
        reasons.append('symlink_path')
    if not cwd_complete:
        reasons.append('process_cwds_unknown')
    row['in_use'] = any(within(c, [os.path.realpath(path)]) for c in cwds)
    if row['in_use']:
        reasons.append('process_in_use')
    if row['tool'] == 'orca' and not shutil.which('orca'):
        reasons.append('orca_unavailable')
    if not row['exists']:
        reasons.append('missing_directory')
        # Only an actual absent path, not permission/IO errors, is prune evidence.
        try:
            os.lstat(path)
        except FileNotFoundError:
            if set(reasons) <= {'missing_directory'}:
                row['category'] = 'prune'
        except OSError:
            reasons.append('path_unknown')
        return row
    try:
        # Confirm that a replacement directory is still the registered worktree.
        if git(path, 'rev-parse', '--show-toplevel', deadline=deadline).strip() != path:
            raise ValueError('worktree identity mismatch')
        def common_dir(directory):
            value = git(directory, 'rev-parse', '--git-common-dir', deadline=deadline).strip()
            return os.path.realpath(value if os.path.isabs(value) else os.path.join(directory, value))
        if common_dir(path) != common_dir(repo):
            raise ValueError('registered repository identity mismatch')
        row['head'] = git(path, 'rev-parse', 'HEAD', deadline=deadline).strip()
        if row['head'] != entry.get('HEAD'):
            raise ValueError('HEAD changed during inventory')
        row['dirty'] = bool(git(path, 'status', '--porcelain=v1', '--untracked-files=all',
                                '--ignore-submodules=none', deadline=deadline))
        if row['dirty']:
            reasons.append('dirty_or_untracked')
        row['ignored_paths'], row['ignored_count'] = ignored_files(path, cfg, deadline)
        if row['ignored_count']:
            reasons.append('ignored_files')
        if os.path.lexists(os.path.join(path, '.gitmodules')):
            reasons.append('submodules')
        row['unpushed'] = int(git(path, 'rev-list', '--count', 'HEAD', '--not', '--remotes', deadline=deadline))
        if row['unpushed']:
            reasons.append('unpushed')
        row['merged'] = ancestor(path, row['head'], default_ref(repo, deadline), deadline)
        if not row['branch'] and row['merged'] is not True:
            reasons.append('detached_unmerged')
        if online:
            row['pr_state'] = pr_state(repo, row['branch'], row['head'], deadline)
        last = activity(path, deadline)
        # Worktree-specific reflog includes checkout/reset activity.
        log = git(path, 'rev-parse', '--git-path', 'logs/HEAD', deadline=deadline).strip()
        log = log if os.path.isabs(log) else os.path.join(path, log)
        if os.path.exists(log):
            last = max(last, os.stat(log).st_mtime)
        row['last_activity'] = datetime.datetime.fromtimestamp(last, datetime.timezone.utc).isoformat()
        row['idle_days'] = max(0, (time.time() - last) / 86400)
        idle = row['idle_days'] >= (days if days is not None else cfg['worktrees']['idle_days'])
        if not (row['merged'] or row['pr_state'] in ('MERGED', 'CLOSED') or idle):
            reasons.append('not_merged_or_idle')
        if not reasons:
            row['category'] = 'worktrees'
        elif (set(reasons) <= {'main_worktree', 'not_merged_or_idle'} and
              row['idle_days'] >= cfg['worktrees']['stale_days']):
            row['category'] = 'slim'
    except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired) as error:
        reasons.append('query_failed')
        row['error'] = str(error)
    return row


def branch_rows(repo, entries, cfg, deadline):
    target = default_ref(repo, deadline)
    used = {e.get('branch') for e in entries}
    rows = []
    raw = git(repo, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads/', deadline=deadline)
    for line in raw.splitlines():
        remaining(deadline, 1)
        ref, head = line.split()
        reasons = []
        if ref in used:
            reasons.append('checked_out')
        if path_protected(repo, cfg) or not cfg['valid']:
            reasons.append('protected_or_invalid_config')
        if target and ref.removeprefix('refs/heads/') == target.removeprefix('refs/remotes/origin/'):
            reasons.append('default_branch')
        merged = ancestor(repo, head, target, deadline)
        if not merged:
            reasons.append('not_merged')
        unpushed = int(git(repo, 'rev-list', '--count', head, '--not', '--remotes', deadline=deadline))
        if unpushed:
            reasons.append('unpushed')
        rows.append({'repo': repo, 'branch': ref.removeprefix('refs/heads/'), 'head': head,
                     'merged': merged, 'unpushed': unpushed, 'reasons': reasons,
                     'category': 'manual' if reasons else 'branches'})
    return rows


class SizeCache:
    def __init__(self):
        self.path = Path(history.state_dir())/'worktree-sizes.json'
        try:
            self.data = json.loads(self.path.read_text())
            if not isinstance(self.data, dict):
                self.data = {}
        except (OSError, ValueError):
            self.data = {}

    def measure(self, path, deadline):
        stamp = os.stat(path).st_mtime_ns
        old = self.data.get(path, {})
        # Root mtime cannot detect nested changes; expire estimates after 24 hours.
        if (isinstance(old, dict) and old.get('mtime') == stamp and
                isinstance(old.get('at'), (int, float)) and isinstance(old.get('bytes'), int) and
                old['bytes'] >= 0 and 0 <= time.time()-old['at'] < 86400):
            return old['bytes'], True, True
        size, complete = disk.size_of(path, deadline)
        if complete:
            self.data[path] = {'mtime': stamp, 'at': time.time(), 'bytes': size}
        return size, complete, False

    def save(self):
        name = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', dir=self.path.parent, delete=False) as f:
                name = f.name
                json.dump(self.data, f)
            os.replace(name, self.path)
        except OSError:
            if name:
                try:
                    os.unlink(name)
                except OSError:
                    pass


def artifact_safe(path, worktree, cfg, deadline):
    if (path_protected(path, cfg) or
            disk.deletion_evidence(path, cfg['workspaces']) != worktree or
            os.path.lexists(os.path.join(path, '.git'))):
        return False
    errors = []
    for root, dirs, files in os.walk(path, followlinks=False, onerror=errors.append):
        remaining(deadline, 1)
        if '.git' in dirs or '.git' in files:
            return False
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
    if errors:
        raise OSError('artifact boundary scan incomplete')
    relative = os.path.relpath(path, worktree)
    # Generated directory names are not proof that their contents are untracked.
    return (not git(worktree, 'ls-files', '-z', '--', relative, deadline=deadline) and
            not git(worktree, 'status', '--porcelain=v1', '--untracked-files=all',
                    '--ignore-submodules=none', '--', relative, deadline=deadline))


def artifacts(path, deadline):
    for root, dirs, _ in os.walk(path, followlinks=False):
        remaining(deadline, 1)
        dirs[:] = [d for d in dirs if d != '.git' and not os.path.islink(os.path.join(root, d))
                   and not os.path.lexists(os.path.join(root, d, '.git'))]
        for name in list(dirs):
            if name in disk.ARTIFACTS:
                dirs.remove(name)
                yield os.path.join(root, name)


def collect(cfg=None, budget=60, days=None, online=False, fetch=False):
    cfg = cfg or load_config()
    deadline = time.monotonic() + budget
    result = {'items': [], 'branches': [], 'artifacts': [], 'complete': True, 'errors': []}
    cache = SizeCache()
    try:
        repos, errors = discover(cfg['workspaces'], min(deadline, time.monotonic()+min(10, budget/3)))
        result['errors'].extend(errors)
        cwds, cwd_complete = process_snapshot(min(deadline, time.monotonic()+5))
        result['process_cwds_complete'] = cwd_complete
        result['complete'] &= cwd_complete
        evidence_deadline = deadline - min(20, budget/3)
        for repo, entries in repos:
            if fetch:
                git(repo, 'fetch', '--all', deadline=deadline)
                entries = registrations(repo, deadline)
            main = entries[0]['worktree']
            for entry in entries:
                row = inspect(repo, entry, main, cfg, cwds, cwd_complete,
                              min(evidence_deadline, time.monotonic()+2), days, online)
                result['items'].append(row)
                if UNKNOWN_REASONS.intersection(row['reasons']):
                    result['complete'] = False
            try:
                result['branches'].extend(branch_rows(repo, entries, cfg, min(evidence_deadline, time.monotonic()+3)))
            except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired) as error:
                result['errors'].append({'repo': repo, 'error': str(error)})
        # Measure after inventory so expensive size scans do not hide registered worktrees.
        for row in sorted(result['items'], key=lambda r: r['category'] not in ('worktrees', 'slim')):
            if not row['exists']:
                continue
            remaining(deadline, 1)
            row['bytes'], row['size_complete'], row['size_cached'] = cache.measure(row['path'], min(deadline, time.monotonic()+1))
            if not row['size_complete']:
                result['complete'] = False
            if row['category'] == 'slim':
                for path in artifacts(row['path'], deadline):
                    if artifact_safe(path, row['path'], cfg, deadline):
                        size, complete, cached = cache.measure(path, deadline)
                        result['artifacts'].append({'path': path, 'repo': row['repo'], 'worktree': row['path'],
                                                    'bytes': size, 'size_complete': complete, 'size_cached': cached,
                                                    'category': 'slim'})
                        result['complete'] &= complete
    except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired) as error:
        result['errors'].append(str(error))
        result['complete'] = False
    cache.save()
    result['complete'] &= not bool(result['errors'])
    result['summary'] = summary(result)
    return result


def summary(data):
    counts = {k: sum(r['category'] == k for r in data['items']) for k in ('worktrees', 'prune', 'slim', 'manual')}
    # Unknown is a subset of manual, not an additional cleanup category.
    counts['unknown'] = sum(bool(UNKNOWN_REASONS.intersection(r['reasons'])) for r in data['items'])
    counts['branches'] = sum(r['category'] == 'branches' for r in data['branches'])
    counts['artifacts'] = len(data['artifacts'])
    return {'worktree_count': len(data['items']), 'counts': counts,
            'reclaimable_bytes_estimate': sum(r.get('bytes') or 0 for r in data['items'] if r['category'] == 'worktrees') +
            sum(r.get('bytes') or 0 for r in data['artifacts'])}


def fresh_row(row, cfg, deadline, days, online):
    entries = registrations(row['repo'], deadline)
    entry = next((e for e in entries if e['worktree'] == row['path']), None)
    if not entry:
        raise ValueError('registration disappeared')
    cwds, complete = process_snapshot(deadline)
    fresh = inspect(row['repo'], entry, entries[0]['worktree'], cfg, cwds, complete, deadline, days, online)
    if fresh['head'] != row['head'] or fresh['branch'] != row['branch']:
        raise ValueError('worktree identity changed')
    return fresh


def clean(data, yes=False, include=None, days=None, online=False):
    include = KINDS if include is None else set(include)
    if not include <= KINDS:
        raise ValueError('unknown cleanup kind')
    outcomes, pruned = [], set()
    targets = [(r['category'], r) for r in data['items'] if r['category'] in ('worktrees', 'prune')]
    targets += [('branches', r) for r in data['branches'] if r['category'] == 'branches']
    targets += [('slim', r) for r in data['artifacts']]
    for kind, row in targets:
        if kind not in include:
            continue
        record = {'mode': 'worktrees', 'kind': kind, 'path': row.get('path'),
                  'repo': row['repo'], 'branch': row.get('branch'), 'result': 'dry-run'}
        try:
            if yes:
                cfg, deadline = load_config(), time.monotonic()+20
                if not cfg['valid'] or not within(row['repo'], cfg['workspaces']):
                    raise ValueError('invalid config or repository no longer in configured roots')
                if kind == 'worktrees':
                    fresh = fresh_row(row, cfg, deadline, days, online)
                    if fresh['category'] != kind:
                        raise ValueError(','.join(fresh['reasons']))
                    # git remove refuses dirty trees/submodules. Orca must preserve its metadata.
                    argv = (['orca', 'worktree', 'rm', '--worktree', 'path:'+row['path'], '--json']
                            if fresh['tool'] == 'orca' else
                            ['git', '--no-optional-locks', '-C', row['repo'], 'worktree', 'remove', '--', row['path']])
                elif kind == 'prune':
                    if row['repo'] in pruned:
                        continue
                    entries = registrations(row['repo'], deadline)
                    cwds, complete = process_snapshot(deadline)
                    missing = [e for e in entries if not os.path.isdir(e['worktree'])]
                    checks = [inspect(row['repo'], e, entries[0]['worktree'], cfg, cwds, complete, deadline, days, online) for e in missing]
                    if not checks or any(r['category'] != 'prune' for r in checks):
                        raise ValueError('repository has unsafe missing registrations')
                    # Prune operates repository-wide; all missing registrations were checked.
                    argv = ['git', '--no-optional-locks', '-C', row['repo'], 'worktree', 'prune', '--expire', 'now']
                    pruned.add(row['repo'])
                elif kind == 'branches':
                    entries = registrations(row['repo'], deadline)
                    current = next((r for r in branch_rows(row['repo'], entries, cfg, deadline) if r['branch'] == row['branch']), None)
                    if not current or current['category'] != kind or current['head'] != row['head']:
                        raise ValueError('branch changed or no longer eligible')
                    argv = ['git', '--no-optional-locks', '-C', row['repo'], 'branch', '-d', '--', row['branch']]
                else:
                    parent = next(r for r in data['items'] if r['path'] == row['worktree'])
                    if fresh_row(parent, cfg, deadline, days, online)['category'] != 'slim':
                        raise ValueError('repository no longer stale and safe')
                    if not artifact_safe(row['path'], row['worktree'], cfg, deadline):
                        raise ValueError('artifact is tracked, protected, or lacks project evidence')
                    history.append('worktrees', dict(record, result='attempt'))
                    if not disk.clean(row['path'], yes=True):
                        raise ValueError('artifact safety check refused cleanup')
                    argv = None
                if argv:
                    history.append('worktrees', dict(record, result='attempt'))
                    run(argv, deadline)
                record['result'] = 'cleaned'
        except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired, StopIteration) as error:
            record.update(result='skipped', reason=str(error))
        history.append('worktrees', record)
        outcomes.append(record)
    return {'dry_run': not yes, 'outcomes': outcomes, 'summary': data['summary'], 'complete': data['complete']}


def weekly_summary(notifier=None, cfg=None):
    cfg = cfg or load_config()
    key, now = history.state_dir(), time.monotonic()
    if key in _weekly_attempts and now - _weekly_attempts[key] < 7*86400:
        return None
    try:
        if history.read(['worktree-summary'], days=7):
            return None
    except OSError:
        _weekly_attempts[key] = now
        return None
    # Record before collection/append: failures must not retrigger every tick.
    _weekly_attempts[key] = now
    data = collect(cfg=cfg, budget=WEEKLY_BUDGET)
    record = dict(data['summary'], complete=data['complete'])
    history.append('worktree-summary', record)
    if notifier and cfg['notify']['enabled']:
        notifier.send([{'kind': 'WORKTREE_SUMMARY', 'msg':
                        '%sWorktree reclaimable estimate: %.2f GiB (%s)' %
                        ('部分盘点 / Partial inventory: ' if not record['complete'] else '',
                         record['reclaimable_bytes_estimate']/1024**3, record['counts'])}], cfg=cfg)
    return record
