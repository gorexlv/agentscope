#!/usr/bin/env python3
"""
agentscope — 本地 AI Agent 会话与后台进程监控/清理工具

解决的核心问题：
  Claude Code / Codex 等 CLI Agent 会话结束后，其派生的子进程
  （MCP server、dev server、language server、node 进程等）不会随之退出，
  变成 ppid=1 的孤儿，长期堆积直到拖垮整台机器。

原理（已在 macOS 26.6 arm64 实测验证）：
  - 子进程继承父进程的进程组（pgid）
  - 父进程退出后子进程成为孤儿，但保留原 pgid
  - killpg(pgid) 可一次性终止整组残留进程

用法：
  agentscope scan              列出 Agent 会话与残留进程组
  agentscope scan --json       JSON 输出
  agentscope watch             持续监控，检测到孤儿组即清理
  agentscope clean             清理孤儿进程组（需 --yes 确认）
  agentscope clean --dry-run   预演，不实际终止
  agentscope tree <pgid>       查看某个进程组的完整构成
"""

import argparse
import collections
import json
import os
import signal
import subprocess
import sys
import time

VERSION = "1.1.0"

# ─────────────────────────────────────────────────────────────
# libproc 绑定：绕过 ps/top 的沙箱限制，直接读内核进程表
# ─────────────────────────────────────────────────────────────

from agentscope_lib.procstat import list_pids, proc_bsdinfo, scan_processes, proc_pgid, load_cwds, load_listening_ports
from agentscope_lib.config import load_config, protected as config_protected, within
from agentscope_lib.history import append as audit
from agentscope_lib.lifecycle import remaining
from agentscope_lib.ownership import orca_managed
from agentscope_lib.ports import abandoned
from agentscope_lib.notify import Notifier, group_key, expire_seen
import functools
import builtins
print = functools.partial(builtins.print, flush=True)

# ─────────────────────────────────────────────────────────────
# Agent / 开发工具进程特征
# ─────────────────────────────────────────────────────────────

AGENT_MARKERS = (
    # CLI Agent 本体
    "claude", "codex", "zcode", "kimi", "cursor-agent", "aider",
    "gemini", "qwen", "opencode", "goose", "amp",
    # MCP server
    "mcp-server", "mcp_server", "mcp-server-darwin",
    # 语言服务 / 构建
    "language-server", "typescript", "tsserver", "esbuild", "vite",
    "node", "deno", "bun",
    # 包管理 / 任务
    "pnpm", "npm", "yarn", "tsup", "tsx",
    # 交互 shell（Agent 常通过 fish/zsh 起子进程）
    "fish", "zsh", "bash",
)

# 检测到就说明是"宿主型"进程（不该被当成 agent 残留）
DESKTOP_HOSTS = ("workbuddy", "logseq", "chatgpt", "lark", "orca",
                 "paseo", "raycast", "orbstack", "iterm")

# Agent 会话通常在"工作区目录"里跑。这些路径前缀下的 cwd 视为工作区。
# 可用环境变量 AGENTSCOPE_WORKSPACES 覆盖（冒号分隔）。
# 明确排除的宿主型进程名（桌面应用 Helper，不是 CLI Agent）
EXCLUDE_COMM = (
    "browser helper", "ego helper", "codex (service)", "codex (renderer",
    "claude helper", "lark helper", "electron helper", "workbuddy helper",
    "logseq helper", "helper (renderer", "helper (plugin)",
)


_cwd_cache = {}


def load_cwds_batch(pids, deadline=None):
    """一次性获取所有进程的 cwd —— 比逐个调用 lsof 快 60 倍以上。

    逐进程调 lsof 在 500+ 进程时需要 24 秒，hook 有超时限制，必须批量。
    用 lsof 的机器可读格式 -F pn：先输出 p<pid>，再输出 n<cwd>。
    """
    global _cwd_cache
    _cwd_cache = {p: "" for p in pids}
    _cwd_cache.update(load_cwds(pids, deadline=deadline))
    return _cwd_cache


def proc_cwd(pid):
    if pid in _cwd_cache:
        return _cwd_cache[pid]
    val = ""
    try:
        r = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                           capture_output=True, text=True, timeout=10).stdout
        for line in r.splitlines():
            if line.startswith("n"):
                val = line[1:]
                break
    except Exception:
        pass
    _cwd_cache[pid] = val
    return val


def app_of(pid):
    """从进程打开的文件路径反推宿主 .app"""
    if _app_cache is not None:
        return _app_cache.get(pid, set())
    apps = set()
    try:
        r = subprocess.run(["lsof", "-p", str(pid)], capture_output=True,
                           text=True, timeout=15).stdout
    except Exception:
        return apps
    for line in r.splitlines():
        if ".app/Contents/" in line:
            name = line.split(".app/Contents/")[0].split("/")[-1]
            if name:
                apps.add(name)
    if _app_cache is not None:
        _app_cache[pid] = apps
    return apps


_app_cache = None


def load_apps_batch(pids, deadline=None):
    """批量获取所有进程的宿主 .app —— 一次 lsof 调用"""
    global _app_cache
    _app_cache = {}
    if not pids:
        return _app_cache
    try:
        r = subprocess.run(["lsof", "-p", ",".join(str(p) for p in pids)],
                           capture_output=True, text=True, timeout=remaining(deadline, 30)).stdout
        for line in r.splitlines()[1:]:
            if ".app/Contents/" not in line:
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                name = line.split(".app/Contents/")[0].split("/")[-1]
                if name:
                    _app_cache.setdefault(int(parts[1]), set()).add(name)
    except Exception:
        pass
    return _app_cache


# ─────────────────────────────────────────────────────────────
# 分析逻辑
# ─────────────────────────────────────────────────────────────

def build_groups(info):
    groups = collections.defaultdict(list)
    for pid, b in info.items():
        groups[b["pgid"]].append(pid)
    return groups


_ports_cache = None


def load_ports_batch(deadline=None):
    """批量获取所有监听端口 —— 一次 lsof 调用搞定"""
    global _ports_cache
    _ports_cache = load_listening_ports(deadline=deadline)
    return _ports_cache


_tty_cache = None


def tty_pids():
    """持有终端设备的进程集合 —— 交互式 Agent 会话的特征"""
    global _tty_cache
    if _tty_cache is not None:
        return _tty_cache
    out = set()
    try:
        r = subprocess.run(
            ["lsof", "-a", "-c", "node", "-c", "fish", "-c", "zsh", "-c", "bash",
             "-c", "claude.exe", "-c", "claude", "-c", "codex", "-c", "zcode-cli",
             "-c", "kimi-code", "-c", "head", "-c", "tail",
             "-d", "0", "-d", "1", "-d", "2"],
            capture_output=True, text=True, timeout=20).stdout
        for line in r.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                out.add(int(parts[1]))
    except Exception:
        pass
    _tty_cache = out
    return out


# Agent 本体进程名 —— 这些还活着说明会话还在用
AGENT_MAIN = ("claude", "codex", "zcode-cli", "kimi-code", "cursor-agent",
              "opencode", "aider", "gemini")


def is_agent_main(b):
    hay = (b["comm"] + " " + b["name"]).lower()
    return any(m in hay for m in AGENT_MAIN)


def is_agentish(b):
    """进程本身像不像 Agent 生态的成员。

    判据要精准，避免把桌面应用的 Helper 误判进来：
      - 排除已知的桌面宿主 Helper
      - 命中 AGENT_MARKERS 中的关键词
    """
    hay = (b["comm"] + " " + b["name"]).lower()
    for ex in EXCLUDE_COMM:
        if ex in hay:
            return False
    return any(m in hay for m in AGENT_MARKERS)


def in_workspace(path):
    """cwd 是否位于工作区目录中 —— CLI Agent 的关键特征"""
    return within(path, load_config()['workspaces'])


def parent_exited(ppid, info):
    if ppid == 1:
        return True
    if ppid <= 0 or ppid in info:
        return False
    try:
        os.kill(ppid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def classify_group(pgid, members, info, cwd_query=None, ports_query=None, tty_query=None, app_query=None, cfg=None, parent_query=None):
    """给一个进程组分类，返回结构化描述"""
    cfg = cfg or load_config()
    parent_query = parent_query or (lambda ppid: parent_exited(ppid, info))
    cwd_query = cwd_query or proc_cwd
    ports_query = ports_query or (lambda: _ports_cache)
    tty_query = tty_query or tty_pids
    app_query = app_query or app_of
    names = [info[p]["comm"] for p in members]

    # 孤儿判定 —— 不能只看 ppid==1。
    #
    # macOS 上所有桌面应用都是 launchd 拉起后 reparent 到 init 的，
    # 根进程 ppid 同样是 1（Arc / Claude / ChatGPT / Lark / Logseq 全都如此），
    # 它们完全健康，绝不能清理。
    #
    # 真正的判据是：根进程自身就是一个 shell/开发工具（bash/node/zsh/...），
    # 且它的 cwd 落在工作区目录里 —— 这说明它是一次 Agent 会话
    # 启动的进程组，会话结束后父进程消失，只剩这一组残留。
    roots = [p for p in members if info[p]["ppid"] not in members]
    if not roots:
        roots = [p for p in members if info[p]["ppid"] == 1] or members

    root_cwds = [cwd_query(p) for p in roots]
    root_in_ws = all(within(c, cfg["workspaces"]) for c in root_cwds)
    root_is_shell = all(
        info[p]["comm"].lower() in ("bash", "zsh", "fish", "sh", "node",
                                    "python3", "head", "tail", "deno", "bun")
        for p in roots)
    root_is_desktop = any(
        any(h in (info[p]["comm"] + " " + info[p]["name"]).lower()
            for h in DESKTOP_HOSTS)
        for p in roots)

    # 残留 = 根在 workspace 里，且根是 shell/开发工具（非桌面应用）
    orphan = (root_in_ws and root_is_shell and not root_is_desktop
              and all(parent_query(info[p]["ppid"]) for p in roots))

    ownership = {p: orca_managed(p, info) for p in members}
    protected = any(value is not False for value in ownership.values()) or any(config_protected(info[p], cwd_query(p), cfg) or
                    any(h in (info[p]['comm'] + ' ' + info[p]['name']).lower() for h in DESKTOP_HOSTS) or
                    any(h in app.lower() for app in app_query(p) for h in DESKTOP_HOSTS)
                    for p in members)
    # 桌面应用不该出现在 workspace 目录里；若出现也保守跳过
    if not protected:
        for p in members:
            c = cwd_query(p)
            if c.startswith("/Applications/") or c.startswith("/System/"):
                protected = True
                break

    # 判据 A：进程名像 Agent 生态
    agent_hits = sum(1 for p in members if is_agentish(info[p]))

    # 判据 B：cwd 落在工作区里（更可靠 —— 桌面应用 Helper 的 cwd 是 /）
    ws_members = [p for p in members if within(cwd_query(p), cfg["workspaces"])]
    ws_ratio = len(ws_members) / len(members) if members else 0

    agent_ratio = agent_hits / len(members) if members else 0

    # workspace 归属（取 cwd 最集中的那个）
    cwds = collections.Counter(cwd_query(p) for p in members)
    top_cwd, top_n = (cwds.most_common(1)[0] if cwds else ("", 0))

    # 监听端口 —— 后台服务的直接证据（批量查询所有进程一次完成）
    ports = []
    port_map = ports_query()
    if port_map:
        for p in members:
            ports.extend(port_map.get(p, []))
    ports = sorted(set(ports))

    # 综合判定：既像 Agent 生态，又在工作区里；或本身是孤儿组
    agent_like = (agent_ratio >= 0.5 and ws_ratio >= 0.4 and len(members) >= 2)

    # ── 活跃性判定 ──────────────────────────────────────────
    # 关键安全阀：组里若还有 Agent 本体（claude/codex/...）在跑，
    # 说明会话正在进行，绝不能清理。
    # 交互式 Agent 会话一定持有 tty，进一步强化这个信号。
    ttys = tty_query()
    agent_mains = [p for p in members if is_agent_main(info[p])]
    live_agent = [p for p in agent_mains if p in ttys]
    has_live_session = bool(agent_mains) or bool(live_agent)

    return {
        "pgid": pgid,
        "members": sorted(members),
        "size": len(members),
        "orphan": orphan,
        "protected": protected,
        "agent_ratio": round(agent_ratio, 2),
        "workspace_ratio": round(ws_ratio, 2),
        "agent_like": agent_like,
        "has_live_session": has_live_session,
        "agent_mains": sorted(agent_mains),
        "cwd": top_cwd,
        "cwd_concentration": f"{top_n}/{len(members)}",
        "ports": sorted(set(ports)),
        "ports_unknown": port_map is None,
        "orca_managed": any(value is True for value in ownership.values()),
        "orca_unknown": any(value is None for value in ownership.values()),
        "abandoned_server": bool(ports) and all(abandoned(cwd_query(p), cfg) for p in members if (port_map or {}).get(p)),
        "config_valid": cfg.get("valid", True),
        "names": names[:12],
        "root": roots,
        "root_starts": [(p, info[p].get('start_time', 0)) for p in sorted(roots)],
        "reason": "workspace tool roots with exited parents" if orphan else "live parent or non-workspace tool",
        "identities": {str(p): info[p].get('start_time', 0) for p in members},
    }


def analyze(info=None, cfg=None, deadline=None):
    cfg = cfg or load_config()
    global _tty_cache
    _tty_cache = None
    info = scan_processes(deadline=deadline) if info is None else info
    # 批量预取 cwd 与端口 —— 这是性能关键点
    load_cwds_batch(list(info.keys()), deadline=deadline)
    for pid, b in info.items():
        b["cwd"] = _cwd_cache.get(pid, "")
    load_ports_batch(deadline=deadline)
    load_apps_batch(list(info), deadline=deadline)
    remaining(deadline, 1)
    groups = build_groups(info)

    units = []
    for pgid, members in groups.items():
        if len(members) < 2:
            continue
        g = classify_group(pgid, members, info, cfg=cfg, tty_query=lambda: set())
        # 孤儿组一律纳入（父进程已死 = 明确残留信号）
        # 存活组仅在明确像 Agent 会话时纳入
        if g["orphan"] or g["agent_like"]:
            units.append(g)

    units.sort(key=lambda g: (-g["size"], g["pgid"]))
    return info, units


def my_protected_pids(info, cfg=None):
    """当前会话所在的进程树 —— 绝不能动"""
    cfg = cfg or load_config()
    prot = {os.getpid()}
    p = os.getpid()
    while p in info and info[p]['ppid'] > 1 and info[p]['ppid'] not in prot:
        p = info[p]['ppid']
        prot.add(p)
    groups = {info[p]['pgid'] for p in prot if p in info}
    prot.update(p for p, b in info.items() if b['pgid'] in groups or config_protected(b, _cwd_cache.get(p, ""), cfg))
    while True:
        children = {p for p, b in info.items() if b['ppid'] in prot}
        if children <= prot:
            return prot
        prot.update(children)


# ─────────────────────────────────────────────────────────────
# 输出
# ─────────────────────────────────────────────────────────────

def fmt_size(n):
    for unit in ("B", "K", "M", "G"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}T"


def format_units(units, info, limit=None):
    """把进程组渲染成文本（返回字符串，便于 watch 复用）"""
    if not units:
        return "没有发现 Agent 会话残留进程组。系统是干净的。"

    out = []
    out.append(f"{'─' * 78}")
    out.append(f"发现 {len(units)} 个 Agent 相关进程组")
    out.append(f"{'─' * 78}")
    out.append("")

    shown = units[:limit] if limit else units
    for g in shown:
        if g.get("has_live_session"):
            tag = "活跃会话"
            flag = "· 保留（Agent 仍在运行）"
        elif g["orphan"] and cleanable(g, info):
            tag = "残留组"
            flag = "◆ 可一键清理"
        else:
            tag = "受保护残留" if g["orphan"] else "存活组"
            flag = "· 跳过"
        out.append(f"pgid {g['pgid']:<8} {g['size']:>3} 进程  [{tag}]  {flag}")

        if g["cwd"]:
            out.append(f"    工作目录: {g['cwd'].replace(os.path.expanduser('~'), '~')}")
        if g["ports"]:
            out.append(f"    监听端口: {', '.join(g['ports'][:8])}")
        pnames = [n for n in g["names"] if n]
        if pnames:
            out.append(f"    进程名  : {', '.join(pnames[:7])}")
        out.append(f"    成员 PID: {', '.join(str(p) for p in g['members'][:12])}"
                   f"{' ...' if len(g['members']) > 12 else ''}")
        out.append("")
    return "\n".join(out)


def print_units(units, info, limit=None):
    """打印进程组（保留旧接口）"""
    print(format_units(units, info, limit))


def cmd_scan(args):
    def render_scan(data):
        info, units = data
        out = [f"agentscope v{VERSION} — Agent 会话进程监控"]
        out.append(f"系统总进程: {len(info)}")
        orphan_n = sum(1 for g in units if cleanable(g, info))
        live_n = sum(1 for g in units if not g["orphan"])
        out.append(f"可清理孤儿组: {orphan_n}   存活组: {live_n}")
        if any(g.get("ports_unknown") for g in units):
            out.append("端口信息未知，相关组将跳过清理。")
        out.append(format_units(units, info, limit=args.limit))
        return "\n".join(out)

    if getattr(args, "watch", False):
        from agentscope_lib import watch as watch_mod
        return watch_mod.run_watch(
            analyze, render_scan,
            interval=getattr(args, "interval", 3),
            clear=not getattr(args, "no_clear", False),
            iterations=getattr(args, "iterations", 0))

    info, units = analyze()
    if args.json:
        print(json.dumps({
            "version": VERSION,
            "total_processes": len(info),
            "groups": units,
        }, ensure_ascii=False, indent=2))
        return 0

    print()
    print(render_scan((info, units)))
    return 0


def cmd_tree(args):
    info = scan_processes()
    pgid = args.pgid
    members = [p for p, b in info.items() if b["pgid"] == pgid]
    if not members:
        print(f"pgid {pgid} 没有存活进程")
        return 1
    print(f"\npgid {pgid} 进程组构成 ({len(members)} 个进程)\n")
    for p in sorted(members):
        b = info[p]
        c = proc_cwd(p).replace(os.path.expanduser("~"), "~")
        print(f"  PID {p:>6}  ppid={b['ppid']:<7} {b['comm'][:24]:<24} {c[:44]}")
    return 0


def cleanable(g, info, cfg=None, allow_abandoned=False):
    cfg = cfg or load_config()
    return (cfg.get('valid', True) and g.get('config_valid', True)
            and g['pgid'] > 1 and g['orphan'] and not g['protected']
            and not g['has_live_session'] and (not g['ports'] or (allow_abandoned and g.get('abandoned_server')))
            and not g.get('ports_unknown', True)
            and all(info[p].get('start_time', 0) > 0 and info[p].get('uid') == os.getuid() for p in g['members'])
            and not set(g['members']).intersection(my_protected_pids(info, cfg=cfg)))


def revalidate_group(pgid, cfg, deadline=None):
    """One kernel snapshot, target metadata and ports; never a full analyze per group."""
    info = scan_processes(deadline=deadline)
    members = [p for p, b in info.items() if b['pgid'] == pgid]
    if not members:
        return info, None
    load_cwds_batch(members, deadline=deadline)
    load_apps_batch(members, deadline=deadline)
    ports = load_listening_ports(deadline=deadline)
    g = classify_group(pgid, members, info, cfg=cfg, ports_query=lambda: ports,
                       tty_query=lambda: set())
    return info, g


def kill_group(pgid, dry_run=False, timeout=6, mode='clean', expected=None,
               deadline=None, outcome=None, allow_abandoned=False):
    """Signal only confirmed identities; audit errors never change cleanup results."""
    outcome = outcome if outcome is not None else {}
    cfg = load_config()
    sent = set()
    record = {'mode': mode, 'pgid': pgid, 'members': [], 'reason': ''}
    def finish(result, reason=None):
        outcome['result'] = result
        if reason:
            record['reason'] = reason
        audit('kills', dict(record, result=result))
        return result in ('cleaned', 'gone', 'dry-run')
    if not cfg['valid']:
        return finish('skipped', 'invalid config')
    try:
        remaining(deadline, 1)
        info, g = revalidate_group(pgid, cfg, deadline)
        if not g:
            if expected and all(parent_exited(p, {}) for p in expected['members']):
                return finish('gone', 'confirmed members have exited')
            return finish('skipped', 'group unavailable; exit cannot be confirmed')
        if not cleanable(g, info, cfg=cfg, allow_abandoned=allow_abandoned):
            return finish('skipped', 'revalidation failed')
        if expected and group_key(expected) != group_key(g):
            return finish('skipped', 'root identity changed')
        # Child churn is allowed, but only matching, previously confirmed identities are signalled.
        identities = expected['identities'] if expected else g['identities']
        members = [p for p in g['members'] if identities.get(str(p)) == info[p]['start_time']]
        record.update(members=[{k: info[p].get(k, '') for k in ('pid', 'comm', 'cmdline')} |
                               {'cwd': proc_cwd(p)} for p in members], reason=g['reason'])
        if dry_run:
            print(f'[dry-run] 将终止 pgid {pgid}')
            return finish('dry-run')
        audit('kills', dict(record, result='attempt'))
        def survivors():
            result = []
            for p in members:
                b = proc_bsdinfo(p)
                if b and b.get('status') == 5:  # SZOMB has exited, awaiting reaping.
                    continue
                if b and b.get('start_time') == identities[str(p)] and proc_pgid(p) == pgid:
                    result.append(p)
                elif b is None and not parent_exited(p, {}):
                    result.append(p)  # Unknown is not proof of exit.
            return result
        sent = set()
        def depth(pid):
            visited = set()
            while pid in info and info[pid]['ppid'] in members and pid not in visited:
                visited.add(pid)
                pid = info[pid]['ppid']
            return len(visited)
        def send(sig):
            for p in sorted(members, key=depth, reverse=True):
                remaining(deadline, 1)
                b = proc_bsdinfo(p)
                if b and b.get('status') != 5 and b.get('start_time') == identities[str(p)] and proc_pgid(p) == pgid:
                    try:
                        os.kill(p, sig)
                        sent.add(p)
                    except (ProcessLookupError, PermissionError):
                        pass
        def wait_for_exit(seconds):
            end = time.monotonic() + remaining(deadline, seconds)
            while survivors() and time.monotonic() < end:
                time.sleep(min(.1, max(0, end - time.monotonic())))
        send(signal.SIGTERM)
        wait_for_exit(timeout)
        if survivors():
            send(signal.SIGKILL)
            wait_for_exit(1.5)
        if survivors():
            return finish('remaining')
        # New members were not authorized; report them rather than claiming whole-group success.
        current = scan_processes(deadline=deadline)
        if any(b['pgid'] == pgid and b.get('status') != 5 for b in current.values()):
            return finish('remaining', 'new or remaining group members')
        return finish('cleaned' if sent else 'gone')
    except (TimeoutError, subprocess.TimeoutExpired):
        return finish('remaining' if sent else 'skipped', 'deadline exceeded')


def cmd_clean(args):
    cfg = load_config()
    if not cfg["valid"]:
        audit("kills", {"mode": "clean", "result": "skipped", "reason": "invalid config", "pgid": None, "members": []})
        return 0
    info, units = analyze(cfg=cfg)
    if any(g.get("ports_unknown") for g in units):
        print("端口信息未知，已跳过相关组清理。")
    targets = []
    skipped_live = []
    for g in units:
        if g.get('has_live_session'):
            skipped_live.append(g)
        if cleanable(g, info, cfg=cfg, allow_abandoned=getattr(args, 'abandoned_servers', False)):
            targets.append(g)

    if not targets:
        print("没有需要清理的孤儿进程组。")
        if skipped_live:
            print(f"\n（跳过 {len(skipped_live)} 个含活跃 Agent 会话的组）")
        return 0

    total = sum(g["size"] for g in targets)
    print(f"\n准备清理 {len(targets)} 个孤儿进程组，共 {total} 个进程\n")
    for g in targets:
        pnames = ", ".join(n for n in g["names"][:5] if n)
        print(f"  pgid {g['pgid']:<8} {g['size']:>3} 进程  {g['cwd'].replace(os.path.expanduser('~'), '~')[:40]}")
        print(f"      {pnames}")

    if not args.yes or args.dry_run:
        if args.dry_run:
            for g in targets:
                audit("kills", {"mode": "clean", "pgid": g["pgid"], "members": [dict(info[p], cwd=proc_cwd(p)) for p in g["members"]], "reason": g["reason"], "result": "dry-run"})
        print(f"\n这是 dry-run。加 --yes 实际执行，或 --dry-run 明确跳过。")
        if skipped_live:
            print(f"已跳过 {len(skipped_live)} 个含活跃 Agent 会话的组（安全保护）")
        return 0

    print()
    ok = 0
    for g in targets:
        outcome = {}
        if kill_group(g["pgid"], expected=g, outcome=outcome, allow_abandoned=getattr(args, "abandoned_servers", False)):
            state = '已自行退出' if outcome.get('result') == 'gone' else '已清理'
            print(f"  ✓ pgid {g['pgid']} {state} ({g['size']} 进程)")
            ok += 1
        else:
            print(f"  ✗ pgid {g['pgid']} 部分残留")

    print(f"\n完成：{ok}/{len(targets)} 个进程组已清理，共回收约 {total} 个进程。")
    if skipped_live:
        print(f"跳过 {len(skipped_live)} 个含活跃 Agent 会话的组（安全保护）")
    return 0 if ok == len(targets) else 1


def cmd_watch(args):
    """Run independent rounds; notification/audit failures must not stop cleanup."""
    print(f'agentscope v{VERSION} — 守护模式，每 {args.interval}s 扫描')
    notifier, seen = Notifier(), set()
    try:
        while True:
            try:
                cfg = load_config()
                info, units = analyze(cfg=cfg)
                expire_seen(seen, units)
                try:
                    from agentscope_lib import metrics
                    metrics.write(metrics.sample(info, units))
                except Exception as error:
                    print(f'metrics failed: {error}', file=sys.stderr)
                if not cfg['valid']:
                    audit('kills', {'mode': 'watch', 'pgid': None, 'members': [], 'result': 'skipped', 'reason': 'invalid config'})
                if cfg['notify']['enabled']:
                    try:
                        from agentscope_lib import agents as agents_mod
                        notifier.send(agents_mod.alerts(agents_mod.collect()), cfg=cfg)
                    except Exception as error:
                        print(f'告警采集失败: {error}', file=sys.stderr)
                for g in units:
                    if not cleanable(g, info, cfg=cfg) or group_key(g) in seen:
                        continue
                    outcome = {}
                    ok = kill_group(g['pgid'], dry_run=args.dry_run, mode='watch', expected=g, outcome=outcome)
                    if ok:
                        seen.add(group_key(g))
                    if outcome.get('result') == 'cleaned' and not args.dry_run:
                        try:
                            notifier.send([{'pid': g['pgid'], 'kind': 'WATCH_CLEANED', 'msg': f"已清理 pgid {g['pgid']}"}], cfg=cfg)
                        except Exception as error:
                            print(f'通知失败: {error}', file=sys.stderr)
                    print(f"pgid {g['pgid']}: {outcome.get('result', 'skipped')}")
            except Exception as error:
                print(f'巡检本轮失败，将继续: {error}', file=sys.stderr)
            time.sleep(max(.1, args.interval))
    except KeyboardInterrupt:
        print('已退出守护模式。')
    return 0


def main():
    ap = argparse.ArgumentParser(
        prog="agentscope",
        description="监控与清理本地 AI Agent 会话及其子进程")
    ap.add_argument("--version", action="version", version=f"agentscope {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="列出 Agent 会话与残留进程组")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    p.add_argument("--limit", type=int, default=0, help="最多显示 N 组")
    p.add_argument("--watch", action="store_true", help="持续监控，按间隔刷新")
    p.add_argument("--interval", type=int, default=3,
                   help="--watch 的刷新间隔（秒）")
    p.add_argument("--no-clear", action="store_true",
                   help="--watch 时不清屏，逐块追加（适合管道）")
    p.add_argument("--iterations", type=int, default=0,
                   help="--watch 刷新多少轮后退出（0 = 无限）")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("tree", help="查看某个进程组构成")
    p.add_argument("pgid", type=int)
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser("clean", help="清理孤儿进程组")
    p.add_argument("--abandoned-servers", action="store_true", help="允许清理已确认工作区消失且仍监听端口的孤儿组")
    p.add_argument("--yes", action="store_true", help="确认执行")
    p.add_argument("--dry-run", action="store_true", help="只预演")
    p.add_argument("--keep-ports", action="store_true", default=True,
                   help="保留监听端口的组（默认开启）")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("watch", help="守护模式，周期扫描并自动清理")
    p.add_argument("--interval", type=int, default=30, help="扫描间隔秒数")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--keep-ports", action="store_true", default=True)
    p.set_defaults(func=cmd_watch)

    from agentscope_lib.history import cmd_history
    p = sub.add_parser('history', help='查看清理与会话历史')
    kinds = p.add_mutually_exclusive_group()
    kinds.add_argument('--kills', action='store_true')
    kinds.add_argument('--sessions', action='store_true')
    p.add_argument('--days', type=int)
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_history)

    # Agent 会话监控子命令（agents / panel / node / stats / alerts / whoami）
    try:
        from agentscope_lib.cli import register as _register_agent_cmds
        _register_agent_cmds(sub)
    except ImportError as e:
        print(f"提示：Agent 监控子命令不可用（{e}）", file=sys.stderr)

    from agentscope_lib.extensions import register as register_extensions
    register_extensions(sub)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
