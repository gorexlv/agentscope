"""Bounded disk inventory. Deletion is restricted to explicit generated-artifact paths."""
import os
from pathlib import Path
import shutil
import time
from .config import load_config, within
from .history import append, read
from .ownership import worktree_for

ARTIFACTS = {'node_modules', '.venv', 'dist', 'build', '.next', 'target'}



def deletion_evidence(path, roots):
    parent = Path(path).parent
    worktree = worktree_for(str(parent), roots)
    if not worktree or not (Path(worktree)/'.git').exists():
        return None
    name = Path(path).name
    patterns = {'node_modules':['package.json'], 'dist':['package.json'], '.next':['package.json'],
                'build':['package.json','build.gradle','build.gradle.kts'],
                '.venv':['pyproject.toml','requirements*.txt','setup.py'], 'target':['Cargo.toml','pom.xml']}
    if not any(p.is_file() and not p.is_symlink() for pattern in patterns.get(name,[]) for p in parent.glob(pattern)):
        return None
    return worktree

def size_of(path, deadline):
    total, complete, seen = 0, True, set()
    errors = []
    for root, dirs, files in os.walk(path, followlinks=False, onerror=errors.append):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root,d))]
        if time.monotonic() >= deadline:
            return total, False
        for name in files:
            if time.monotonic() >= deadline:
                return total, False
            try:
                st = os.lstat(os.path.join(root,name))
                identity = (st.st_dev,st.st_ino)
                if identity not in seen:
                    total += st.st_blocks*512
                    seen.add(identity)
            except OSError:
                complete = False
    return total, complete and not errors


def collect(roots=None, budget=8):
    roots = roots if roots is not None else load_config()['workspaces']
    deadline, rows, visited = time.monotonic()+budget, [], set()
    for base in roots:
        if not os.path.isdir(base):
            continue
        base = os.path.realpath(base)
        for root, dirs, _files in os.walk(base, followlinks=False):
            if time.monotonic() >= deadline:
                return {'items':rows,'complete':False}
            depth = os.path.relpath(root,base).count(os.sep)
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root,d)) and d not in {'.git','.cache'}]
            for name in list(dirs):
                path = os.path.join(root,name)
                if name in ARTIFACTS:
                    dirs.remove(name)
                    if path in visited:
                        continue
                    visited.add(path)
                    size, complete = size_of(path,deadline)
                    evidence = deletion_evidence(path,roots)
                    rows.append({'path':path,'worktree':worktree_for(root,roots),'kind':name,'bytes':size,'size_complete':complete,
                                 'abandoned':False,'deletable':bool(evidence),'suggestion':'Review disk --clean PATH --yes (requires no active processes)' if evidence else None})
            meta = Path(root)/'.git/worktrees'
            if meta.is_dir():
                try:
                    entries = list(meta.iterdir())
                except OSError:
                    entries = []
                for entry in entries:
                    try:
                        gitdir = (entry/'gitdir').read_text().strip()
                        worktree = os.path.dirname(gitdir)
                        if os.path.isabs(worktree) and not os.path.exists(worktree):
                            rows.append({'path':str(entry),'worktree':worktree,'kind':'git-worktree-metadata',
                                         'bytes':None,'abandoned':True,'suggestion':'Review git worktree prune --dry-run in repository'})
                    except OSError:
                        continue
            if depth >= 3:
                dirs.clear()
    # History can explain deleted worktrees even if all their artifacts are already gone.
    try:
        old_cwds = {r.get('cwd') for r in read(['sessions']) if r.get('cwd')}
    except OSError:
        old_cwds = set()
    from .ownership import missing_path
    for cwd in sorted(old_cwds):
        if within(cwd, roots) and missing_path(cwd):
            rows.append({'path':cwd,'worktree':cwd,'kind':'deleted-worktree','bytes':0,
                         'abandoned':True,'suggestion':'No directory remains; inspect stale git worktree metadata'})
    return {'items':rows,'complete':True}


def clean(path, yes=False):
    from . import cleanup
    cfg = load_config()
    path = os.path.abspath(os.path.expanduser(path))
    record = {'mode':'disk','pgid':None,'members':[],'path':path}
    def finish(result, reason):
        append('kills', dict(record,result=result,reason=reason))
        return result == 'cleaned'
    if not yes:
        return finish('skipped','explicit --yes required')
    if not cfg['valid'] or not within(path,cfg['workspaces']) or os.path.basename(path) not in ARTIFACTS:
        return finish('skipped','invalid config or non-artifact target')
    if path != os.path.realpath(path) or not os.path.isdir(path) or path in cfg['workspaces']:
        return finish('skipped','symlink, missing target, or workspace root')
    evidence = deletion_evidence(path,cfg['workspaces'])
    if not evidence:
        return finish('skipped','git worktree and matching project marker required')
    before = os.stat(path)
    info, groups = cleanup.analyze(cfg=cfg)
    worktree = worktree_for(os.path.dirname(path),cfg["workspaces"])
    cwds = {p: cleanup.proc_cwd(p) for p in info}
    if any(b.get('uid') == os.getuid() and not cwds[p] for p, b in info.items()):
        return finish('skipped','process working directories are incomplete')
    related = {p for p in info if within(cwds[p],[worktree])}
    # Reuse process safety classification for diagnostics; disk cleanup never kills owners.
    record['related_groups'] = [{'pgid':g['pgid'],'cleanable':cleanup.cleanable(g,info,cfg=cfg)}
                                for g in groups if set(g['members']) & related]
    if related:
        return finish('skipped','worktree has active processes; stop them explicitly first')
    if deletion_evidence(path,cfg['workspaces']) != evidence:
        return finish('skipped','project evidence changed during validation')
    current = os.stat(path)
    if (before.st_dev,before.st_ino) != (current.st_dev,current.st_ino) or os.path.realpath(path) != path:
        return finish('skipped','target changed during validation')
    append('kills',dict(record,result='attempt',reason='explicit generated-artifact deletion'))
    try:
        shutil.rmtree(path)
        return finish('cleaned','generated artifact removed')
    except OSError as error:
        return finish('remaining',str(error))
