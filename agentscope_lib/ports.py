"""Listening-port attribution and conservative abandoned-workspace evidence."""
from . import procstat
from .config import load_config, within
from .ownership import owner_pids, missing_path, worktree_for


def abandoned(cwd, cfg):
    return within(cwd, cfg['workspaces']) and missing_path(cwd)


def collect(info=None, cwds=None, session_rows=None, port_map=None):
    cfg = load_config()
    info = procstat.scan_processes() if info is None else info
    cwds = procstat.load_cwds(list(info)) if cwds is None else cwds
    port_map = procstat.load_listening_ports() if port_map is None else port_map
    if port_map is None:
        return {'ports_unknown':True, 'services':[]}
    rows = []
    for pid, addresses in sorted(port_map.items()):
        b = info.get(pid, {})
        cwd = cwds.get(pid, '')
        owners, confidence = owner_pids(pid, info, session_rows or [])
        if not owners and cwd:
            owners = sorted({s['pid'] for s in (session_rows or []) if s.get('cwd') == cwd and s.get('is_main', True)})
            confidence = 'cwd-ambiguous' if len(owners)>1 else 'cwd' if owners else 'unknown'
        stale = abandoned(cwd, cfg)
        rows.append({'pid':pid, 'pgid':b.get('pgid'), 'addresses':addresses, 'cwd':cwd,'worktree':worktree_for(cwd,cfg['workspaces']),
                     'session_pids':owners, 'attribution':confidence, 'abandoned':stale,
                     'suggestion':'Review clean --abandoned-servers --dry-run' if stale else None})
    return {'ports_unknown':False, 'services':rows}
