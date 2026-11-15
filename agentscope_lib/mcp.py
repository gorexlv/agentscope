"""MCP process inventory; duplicate instances are informational, never kill targets."""
import os
import re
import shlex
from . import procstat
from .ownership import owner_pids


def server_name(record):
    try:
        parts = shlex.split(record.get('cmdline', ''))
    except ValueError:
        return None
    for part in parts:
        base = os.path.basename(part).lower()
        scoped = re.search(r'@([a-z0-9_.-]+)/mcp(?:[/@]|$)', part.lower())
        if scoped:
            return scoped.group(1)
        match = re.search(r'(?:@modelcontextprotocol/server-|mcp-server-|mcp_server_)([a-z0-9_.-]+)', part.lower())
        if match:
            return match.group(1)
        if re.fullmatch(r'[a-z0-9_.-]+-mcp(?:\.js)?', base):
            return base.removesuffix('.js').removesuffix('-mcp')
        if base in ('mcp', 'mcp-server'):
            return 'unknown-mcp'
    return None


def collect(info=None, cwds=None, session_rows=None, with_resources=True):
    info = procstat.scan_processes() if info is None else info
    cwds = procstat.load_cwds(list(info)) if cwds is None else cwds
    session_rows = session_rows or []
    groups = {}
    for pid, b in info.items():
        name = server_name(b)
        if not name:
            continue
        owners, confidence = owner_pids(pid, info, session_rows)
        cwd = cwds.get(pid, '')
        if not owners and cwd:
            owners = sorted({s['pid'] for s in session_rows if s.get('cwd') == cwd and s.get('is_main', True)})
            confidence = 'cwd-ambiguous' if len(owners)>1 else 'cwd' if owners else 'unknown'
        resource = procstat.task_resources(pid) if with_resources else None
        row = {'pid':pid, 'cwd':cwd, 'session_pids':owners, 'attribution':confidence,
               'mem_mb':resource['rss_bytes']/1024**2 if resource else None}
        g = groups.setdefault(name, {'name':name, 'instances':[], 'count':0, 'mem_mb':0, 'memory_complete':True})
        g['instances'].append(row)
        g['count'] += 1
        g['mem_mb'] += row['mem_mb'] or 0
        g['memory_complete'] &= row['mem_mb'] is not None
    return sorted(groups.values(), key=lambda g:(-g['count'],g['name']))


def render(groups):
    if not groups:
        return 'MCP: no recognized server processes'
    return '\n'.join(['MCP instances (RSS; duplicate does not mean abandoned)'] +
        [f"  {g['name']}: {g['count']} instances, {g['mem_mb']:.1f} MB" + (' (partial)' if not g['memory_complete'] else '') for g in groups])
