"""Low-cost watch samples and bounded JSONL history, protected by an advisory lock."""
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from . import procstat
from .history import append, state_dir


def sample(info, units):
    from .agents import detect_agent_type
    sessions = []
    for pid,b in info.items():
        kind = detect_agent_type(b.get('path',''),b.get('comm',''))
        if kind:
            resource = procstat.task_resources(pid)
            sessions.append({'pid':pid,'start_time':b.get('start_time'),'agent_type':kind,'cwd':b.get('cwd'),
                             'rss_bytes':resource['rss_bytes'] if resource else None,
                             'cpu_ticks':resource['cpu_ticks'] if resource else None})
    memory = None
    try:
        result = subprocess.run(['vm_stat'],capture_output=True,text=True,timeout=2)
        page = re.search(r'page size of (\d+) bytes',result.stdout)
        if page and result.returncode==0:
            fields = {k:int(v) for k,v in re.findall(r'^([^:\n]+):\s+(\d+)\.',result.stdout,re.MULTILINE)}
            memory = {'page_size':int(page.group(1)), 'free_bytes':fields.get('Pages free',0)*int(page.group(1)),
                      'active_bytes':fields.get('Pages active',0)*int(page.group(1))}
    except (OSError,subprocess.TimeoutExpired):
        pass
    return {'load':list(os.getloadavg()),'memory':memory,'sessions':sessions,
            'groups':len(units),'orphans':sum(g['orphan'] for g in units)}


def write(record, max_bytes=1024*1024, backups=3):
    try:
        directory = Path(state_dir())
        directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        with open(directory/'metrics.lock','a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            target = directory/'metrics.jsonl'
            if target.exists() and target.stat().st_size >= max_bytes:
                for index in range(backups,0,-1):
                    dest = directory/f'metrics.{index}.jsonl'
                    source = target if index==1 else directory/f'metrics.{index-1}.jsonl'
                    if source.exists():
                        os.replace(source,dest)
            return append('metrics',record)
    except OSError as error:
        print(f'agentscope: metrics unavailable: {error}',file=sys.stderr,flush=True)
        return False


def since(value):
    match = re.fullmatch(r'(\d+)([mhd])',value)
    if match:
        return datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(seconds=int(match[1])*{'m':60,'h':3600,'d':86400}[match[2]])
    result = datetime.datetime.fromisoformat(value.replace('Z','+00:00'))
    return result if result.tzinfo else result.astimezone()


def timeline(value='1h'):
    cutoff = since(value)
    rows = []
    for name in ['metrics.jsonl']+[f'metrics.{i}.jsonl' for i in range(1,4)]:
        try:
            with open(Path(state_dir())/name) as f:
                for line in f:
                    try:
                        row = json.loads(line)
                        if datetime.datetime.fromisoformat(row['at']) >= cutoff:
                            rows.append(row)
                    except (ValueError,TypeError,KeyError):
                        pass
        except FileNotFoundError:
            pass
    return sorted(rows,key=lambda r:r['at'])
