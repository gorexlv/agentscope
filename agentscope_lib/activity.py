"""Heuristic local activity states. No state is permission to terminate a session."""
import json
import os
import time
from .procstat import read_file_tail


def last_events(path):
    events = []
    for line in read_file_tail(path, max_lines=80, max_bytes=256*1024):
        try:
            d = json.loads(line)
            if not isinstance(d, dict):
                continue
            payload = d.get('payload') or d
            message = d.get('message') or {}
            if not isinstance(payload, dict) or not isinstance(message, dict):
                continue
            kind = payload.get('type') or d.get('type') or 'unknown'
            if kind in ('token_count', 'session_meta', 'turn_context', 'file-history-snapshot', 'custom-title') or d.get('subtype') == 'turn_duration':
                continue
            stop = message.get('stop_reason') or payload.get('stop_reason')
            content = message.get('content')
            if isinstance(content, list) and any(isinstance(c, dict) and c.get('name') == 'AskUserQuestion' for c in content):
                kind = 'request_user_input'
            events.append((kind, stop))
        except (ValueError, TypeError):
            pass
    return events


def state(path, cpu_pct=None, now=None):
    now = time.time() if now is None else now
    if not path:
        return {'activity_state':'UNKNOWN', 'state_reason':'no session log'}
    try:
        age = max(0, now-os.stat(path).st_mtime)
    except OSError:
        return {'activity_state':'UNKNOWN', 'state_reason':'session log unavailable'}
    events = last_events(path)
    kind, stop = events[-1] if events else ('unknown', None)
    if kind in ('permission_request','approval_request','request_permissions','approval'):
        status = 'WAITING_PERMISSION'
    elif kind in ('task_complete','turn_complete','agent_turn_complete','request_user_input') or stop in ('end_turn','stop'):
        status = 'WAITING_INPUT'
    elif age > 600 and cpu_pct is not None and cpu_pct >= 20:
        status = 'SUSPECTED_LOOP'
    elif age > 600 and kind in ('tool_use','function_call','user','task_started') and cpu_pct is not None and cpu_pct < 1:
        status = 'SUSPECTED_STALL'
    elif age > 120 and (cpu_pct is None or cpu_pct < 1):
        status = 'IDLE'
    elif events:
        status = 'WORKING'
    else:
        status = 'UNKNOWN'
    return {'activity_state':status, 'state_reason':f'last event={kind}; log age={age:.0f}s; heuristic',
            'last_event_type':kind, 'log_age_s':round(age,1)}


def conflicts(rows):
    grouped = {}
    for row in rows:
        if row.get('cwd') and row.get('is_main', True):
            grouped.setdefault(os.path.realpath(row['cwd']),set()).add(row['pid'])
    return [{'cwd':cwd,'pids':sorted(pids)} for cwd,pids in grouped.items() if len(pids)>1]
