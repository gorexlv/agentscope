"""Daemon snapshot: one JSON document describing local agent sessions.

快照是菜单栏 App 的唯一数据源之一（另一个来源是本地 socket）。
契约：runs/menubar-20261116/contract.md

设计约束：
  - 字段缺失时消费方显示"—"，本模块因此宁可写 null 也不抛异常
  - schema 只在破坏性变更时递增，新增字段向后兼容
  - 写文件一律临时文件 + os.replace + 0600，避免消费方读到半个 JSON
  - 复用既有数据源（agents / activity / cleanup / worktrees），不重复扫描
"""

import datetime
import json
import os
import re
import subprocess
import tempfile

from . import history

SCHEMA = 1

# 契约里的会话状态取值
STATES = ('working', 'waiting', 'idle', 'stuck', 'unknown')

# activity.state() 的启发式状态 → 契约状态
STATE_MAP = {
    'WORKING': 'working',
    'WAITING_INPUT': 'waiting',
    'WAITING_PERMISSION': 'waiting',
    'IDLE': 'idle',
    'SUSPECTED_LOOP': 'stuck',
    'SUSPECTED_STALL': 'stuck',
    'UNKNOWN': 'unknown',
}


def state_dir():
    return history.state_dir()


def snapshot_path():
    return os.path.join(state_dir(), 'snapshot.json')


def now_iso():
    return datetime.datetime.now().astimezone().isoformat()


def _version():
    from . import cleanup
    return cleanup.VERSION


# ─────────────────────────────────────────────────────────────
# 主机负载
# ─────────────────────────────────────────────────────────────

def memory_usage():
    """内存/交换使用率；拿不到就写 null，不猜。"""
    out = {'mem_used_pct': None, 'swap_used_gb': None}
    total = 0
    try:
        result = subprocess.run(['sysctl', '-n', 'hw.memsize'], capture_output=True,
                                text=True, timeout=5)
        total = int(result.stdout.strip() or 0)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        total = 0
    if total > 0:
        try:
            result = subprocess.run(['vm_stat'], capture_output=True, text=True, timeout=5)
            match = re.search(r'page size of (\d+) bytes', result.stdout)
            page = int(match.group(1)) if match else 0
            fields = {k: int(v) for k, v in
                      re.findall(r'^([^:\n]+):\s+(\d+)\.', result.stdout, re.MULTILINE)}
            keys = ('Pages active', 'Pages wired down', 'Pages occupied by compressor')
            if page and all(key in fields for key in keys):
                used = sum(fields[key] for key in keys) * page
                out['mem_used_pct'] = round(max(0.0, min(100.0, used / total * 100)), 1)
        except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
            pass
    try:
        result = subprocess.run(['sysctl', '-n', 'vm.swapusage'], capture_output=True,
                                text=True, timeout=5)
        match = re.search(r'used = ([\d.]+)M', result.stdout)
        if match:
            out['swap_used_gb'] = round(float(match.group(1)) / 1024.0, 2)
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
        pass
    return out


def host_load():
    """系统负载 —— 复用 cli.system_load()，再补内存/交换。"""
    from . import cli
    load = {}
    try:
        load = cli.system_load() or {}
    except Exception:
        load = {}
    host = {
        'load1': load.get('load_1m'),
        'load5': load.get('load_5m'),
        'load15': load.get('load_15m'),
        'ncpu': load.get('cores'),
        'mem_used_pct': None,
        'swap_used_gb': None,
    }
    host.update(memory_usage())
    return host


# ─────────────────────────────────────────────────────────────
# 可回收空间（只读缓存，不做全量盘点）
# ─────────────────────────────────────────────────────────────

def _reclaim_from_history():
    """B2 的 worktree 报告缓存：history 里的 worktree-summary 记录。"""
    try:
        rows = history.read(['worktree-summary'], days=30)
    except OSError:
        rows = []
    if not rows:
        return None
    row = rows[-1]
    counts = row.get('counts') or {}
    removable = (counts.get('worktrees', 0) + counts.get('prune', 0)
                 + counts.get('artifacts', 0))
    return {
        'worktrees_gb': round((row.get('reclaimable_bytes_estimate') or 0) / (1024 ** 3), 2),
        'removable': int(removable),
        'updated_at': row.get('at'),
    }


def reclaim_report():
    """可回收空间 —— 取缓存，绝不在每轮巡检里全量盘点 worktree。"""
    for source in (_reclaim_from_history,):
        try:
            report = source()
        except Exception:
            report = None
        if report:
            return report
    return {'worktrees_gb': None, 'removable': 0, 'updated_at': None}


# ─────────────────────────────────────────────────────────────
# 会话
# ─────────────────────────────────────────────────────────────

def session_state(record):
    return STATE_MAP.get(record.get('activity_state') or 'UNKNOWN', 'unknown')


def session_rows(sessions):
    """契约里的会话条目：只保留会话主进程，避免 helper 重复。"""
    from . import agents
    rows = []
    for record in agents.aggregate_sessions(sessions):
        if not record.get('is_main', True):
            continue
        rows.append({
            'pid': record.get('pid'),
            'session_id': record.get('session_id'),
            'project': record.get('project'),
            'processes': record.get('processes'),
            'agent': record.get('agent_type'),
            'model': record.get('model'),
            'cwd': record.get('cwd'),
            'branch': record.get('git_branch'),
            'state': session_state(record),
            'cpu_pct': record.get('cpu_pct'),
            'mem_mb': record.get('mem_mb'),
            'context_pct': record.get('context_pct'),
            'cost_usd': record.get('cost'),
        })
    return rows


def totals(sessions, rows=None):
    from . import agents
    aggregate = agents.totals(sessions)
    return {
        'sessions': aggregate.get('sessions', 0),
        'cost_usd': round(aggregate.get('cost', 0.0) or 0.0, 4),
        'tokens': (aggregate.get('input_tokens', 0) or 0) + (aggregate.get('output_tokens', 0) or 0),
    }


def alert_rows(alerts):
    from . import agents
    return [{'count': item.get('count'), 'pids': item.get('pids'),
             'level': item.get('level'), 'kind': item.get('kind'),
             'pid': item.get('pid'), 'msg': item.get('msg')} for item in agents.aggregate_alerts(alerts)]


# ─────────────────────────────────────────────────────────────
# 快照
# ─────────────────────────────────────────────────────────────

def default_daemon():
    return {'pid': None, 'version': _version(), 'interval_s': None, 'last_clean_at': None}


def build_snapshot(daemon=None, sessions=None, info=None, units=None,
                   alerts=None, reclaim=None, load=None):
    """按契约组装快照。

    参数均可注入，便于复用已有数据源并让测试不依赖真实机器状态。
    """
    from . import agents
    from . import cleanup

    daemon = dict(daemon) if daemon else default_daemon()
    daemon.setdefault('pid', None)
    daemon.setdefault('version', _version())
    daemon.setdefault('interval_s', None)
    daemon.setdefault('last_clean_at', None)

    if sessions is None:
        try:
            sessions = agents.collect()
        except Exception:
            sessions = []
    if alerts is None:
        try:
            alerts = agents.alerts(sessions)
        except Exception:
            alerts = []

    if info is None or units is None:
        try:
            info, units = cleanup.analyze()
        except Exception:
            info, units = ({}, [])

    orphans = [group for group in units if group.get('orphan')]
    cleanable_groups = 0
    for group in orphans:
        try:
            if cleanup.cleanable(group, info):
                cleanable_groups += 1
        except Exception:
            pass

    rows = session_rows(sessions)
    return {
        'schema': SCHEMA,
        'generated_at': now_iso(),
        'daemon': daemon,
        'host': ({key: load.get(key) for key in ('load1', 'load5', 'load15',
                 'ncpu', 'mem_used_pct', 'swap_used_gb')} if load is not None else host_load()),
        'sessions': rows,
        'totals': totals(sessions, rows),
        'alerts': alert_rows(alerts),
        'orphans': {
            'groups': len(orphans),
            'processes': sum(group.get('size', 0) for group in orphans),
            'cleanable_groups': cleanable_groups,
        },
        'reclaim': reclaim if reclaim is not None else reclaim_report(),
    }


def write_snapshot(data, path=None):
    """原子写快照：临时文件 → os.replace → 0600。"""
    path = path or snapshot_path()
    directory = os.path.dirname(path) or '.'
    os.makedirs(directory, mode=0o700, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix='.snapshot-', dir=directory)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, default=str)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return path


def read_snapshot(path=None):
    path = path or snapshot_path()
    try:
        with open(path, encoding='utf-8') as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def render(data):
    """人读的摘要（`agentscope snapshot` 默认输出）。"""
    lines = [f"agentscope 快照 · schema {data.get('schema')} · {data.get('generated_at')}"]
    host = data.get('host') or {}
    lines.append("负载: {}/{}  核 · 内存 {}% · 交换 {} GB".format(
        host.get('load1', '—'), host.get('ncpu', '—'),
        host.get('mem_used_pct', '—'), host.get('swap_used_gb', '—')))
    totals_data = data.get('totals') or {}
    lines.append("会话: {} · 成本 ${} · tokens {}".format(
        totals_data.get('sessions', 0), totals_data.get('cost_usd', 0),
        totals_data.get('tokens', 0)))
    orphans = data.get('orphans') or {}
    lines.append("孤儿组: {} 组 / {} 进程（可清理 {}）".format(
        orphans.get('groups', 0), orphans.get('processes', 0),
        orphans.get('cleanable_groups', 0)))
    reclaim = data.get('reclaim') or {}
    lines.append("可回收: {} GB · {} 项 · 更新于 {}".format(
        reclaim.get('worktrees_gb', '—'), reclaim.get('removable', 0),
        reclaim.get('updated_at') or '—'))
    alerts = data.get('alerts') or []
    lines.append("告警: {}".format(len(alerts)))
    return '\n'.join(lines)
