"""Offline health checks; output contains no credential values or transcript text."""
import json
import os
import re
import time
from pathlib import Path
from . import sessions
from .procstat import read_file_tail


def credential(path, kind):
    row = {'kind':kind,'path':str(path),'status':'missing'}
    try:
        data = json.loads(path.read_text())
        if not isinstance(data,dict):
            raise ValueError('object expected')
        token_data = data.get('claudeAiOauth') if kind=='claude' else data.get('tokens')
        token_data = token_data if isinstance(token_data,dict) else {}
        present = bool(token_data.get('accessToken') or token_data.get('access_token') or data.get('OPENAI_API_KEY'))
        expiry = token_data.get('expiresAt')
        row['status'] = 'present-unverified' if present else 'no-recognized-credential'
        if isinstance(expiry,(int,float)):
            row['expired_hint'] = expiry/1000 < time.time()
    except FileNotFoundError:
        pass
    except (OSError,ValueError,TypeError):
        row['status'] = 'unreadable-or-invalid'
    return row


def quota(home):
    rows = []
    for session in sorted(sessions.codex_session_files(str(home)),key=lambda r:-r['mtime'])[:5]:
        for line in reversed(read_file_tail(session['path'],max_lines=200,max_bytes=256*1024)):
            try:
                item = json.loads(line)
                payload = item.get('payload') or {}
                limits = payload.get('rate_limits') or item.get('rate_limits')
                if not isinstance(limits,dict):
                    continue
                safe = {}
                for window in ('primary','secondary'):
                    values = limits.get(window)
                    if isinstance(values,dict):
                        safe[window] = {k:v for k,v in values.items() if k in ('used_percent','window_minutes','resets_at','reset_at') and isinstance(v,(int,float))}
                if safe:
                    rows.append({'source':session['path'],'mtime':session['mtime'],'limits':safe,'freshness':'historical-local-only'})
                break
            except (ValueError,TypeError,AttributeError):
                continue
    return rows


def collect(home=None):
    home = Path(home or os.path.expanduser('~'))
    hook = {'status':'missing'}
    try:
        data = json.loads((home/'.claude/settings.json').read_text())
        commands = [h.get('command','') for group in data.get('hooks',{}).get('SessionEnd',[]) for h in group.get('hooks',[])]
        hook['status'] = 'configured' if any('agentscope-hook' in c and 'session-end' in c for c in commands) else 'missing-or-overwritten'
    except FileNotFoundError:
        pass
    except (OSError,ValueError,TypeError,AttributeError):
        hook['status'] = 'unreadable-or-invalid'
    notify = {'status':'missing'}
    try:
        content = (home/'.codex/config.toml').read_text()
        line = re.search(r'^notify\s*=\s*(.*)$',content,re.MULTILINE)
        notify['status'] = 'configured' if line and 'codex-notify-wrapper.sh' in line.group(1) else 'missing-or-overwritten'
        wrapper = home/'.local/bin/codex-notify-wrapper.sh'
        body = wrapper.read_text()
        notify['wrapper'] = 'present' if 'agentscope-hook' in body else 'possibly-overwritten'
    except FileNotFoundError:
        notify['wrapper'] = 'missing'
    except OSError:
        notify['wrapper'] = 'unreadable'
    return {'offline':True,'credentials':[credential(home/'.codex/auth.json','codex'), credential(home/'.claude/.credentials.json','claude')],
            'quota':quota(home),'hooks':hook,'notify':notify,
            'note':'Presence and local quota history only; no login or live quota verification performed. Claude Code may use macOS Keychain; a missing credentials file does not imply logged out.'}
