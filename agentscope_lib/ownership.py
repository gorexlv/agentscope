"""Conservative current-ancestry attribution (no environment or credential dumps)."""
import os


def orca_managed(pid, info):
    """True: Orca ancestor, False: chain reaches init, None: unreadable/cyclic chain."""
    seen = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        b = info.get(pid)
        if b is None:
            return None
        comm = b.get('comm', '').lower()
        path = b.get('path', '').lower()
        if comm in ('orca', 'orca helper') or '/orca.app/' in path:
            return True
        pid = b.get('ppid', 0)
        if pid <= 0:
            return None
    return False if pid == 1 else None


def owner_pids(pid, info, session_rows):
    owners = {member: s['pid'] for s in session_rows for member in (s.get('pids') or [s['pid']])}
    seen = set()
    while pid in info and pid not in seen:
        if pid in owners:
            return [owners[pid]], 'ancestor'
        seen.add(pid)
        pid = info[pid].get('ppid')
    return [], 'unknown'


def missing_path(path):
    if not path or not os.path.isabs(path):
        return False
    try:
        os.stat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def worktree_for(cwd, roots):
    """Nearest local .git marker; absent markers retain cwd as an explicit hint."""
    from .config import within
    if not cwd:
        return None
    original = os.path.realpath(cwd)
    path = original
    while within(path, roots):
        if os.path.exists(os.path.join(path,'.git')):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return original
