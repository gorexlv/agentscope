"""
agentscope 的 CLI 扩展：agents / node / stats / alerts 子命令。

与主 CLI（bin/agentscope）配合使用。
"""

import argparse
import json
import os
import sys
import time

from . import agents as agents_mod
from . import nodeheap
from . import procstat
from . import render
from . import sessions
from . import watch as watch_mod

# 直接导入 ANSI 常量：写 RESET 容易漏掉 render. 前缀而变成 NameError
from .render import BOLD, DIM, GRAY, RED, GREEN, YELLOW, CYAN, RESET
from .render import load_line, summary_line


def system_load():
    """系统负载摘要 —— 与会话视图放在一起，便于对照"""
    import subprocess as sp
    try:
        out = sp.run(["sysctl", "-n", "vm.loadavg"], capture_output=True,
                     text=True, timeout=10).stdout.strip()
        load = [float(x) for x in out.strip("{} ").split()]
        cores = int(sp.run(["sysctl", "-n", "hw.ncpu"], capture_output=True,
                           text=True, timeout=10).stdout.strip() or 1)
    except Exception:
        return None
    if not load:
        return None
    return {
        "load_1m": load[0],
        "load_5m": load[1] if len(load) > 1 else None,
        "load_15m": load[2] if len(load) > 2 else None,
        "cores": cores,
        "busy_pct": round(min(100.0, load[0] / cores * 100), 1),
    }


def cmd_agents(args):
    """列出 Agent 会话"""
    sess = agents_mod.collect(context_limit=args.context_limit,
                              with_resources=not args.fast)
    # 按指定字段排序（看 CPU load 时用 --sort cpu）
    key = getattr(args, "sort", "mem")
    if key == "cpu":
        sess.sort(key=lambda r: -(r.get("cpu_pct") or 0))
    elif key == "mem":
        sess.sort(key=lambda r: -(r.get("mem_mb") or 0))
    elif key == "ctx":
        sess.sort(key=lambda r: (r.get("context_pct") is None,
                                 r.get("context_pct") or 0))
    elif key == "cost":
        sess.sort(key=lambda r: -(r.get("cost") or 0))
    elif key == "tokens":
        sess.sort(key=lambda r: -((r.get("input_tokens") or 0) +
                                   (r.get("output_tokens") or 0)))
    if args.json:
        print(json.dumps(sess, ensure_ascii=False, indent=2, default=str))
        return 0

    def render_all(data):
        sl = system_load()
        parts = []
        if sl:
            parts.append(load_line(sl))
            parts.append("")
        parts.append(render.session_table(data))
        parts.append("")
        t = agents_mod.totals(data)
        parts.append(summary_line(t))
        if args.alerts:
            al = agents_mod.alerts(data)
            if al:
                parts.append("")
                parts.append(render.alerts_section(al))
        return "\n".join(parts)

    if getattr(args, "watch", False):
        def collect():
            d = agents_mod.collect(context_limit=args.context_limit,
                                   with_resources=not args.fast)
            return sort_sessions(d, args.sort)
        return watch_mod.run_watch(
            collect, render_all,
            interval=getattr(args, "interval", 3),
            clear=not getattr(args, "no_clear", False),
            iterations=getattr(args, "iterations", 0))

    print()
    print(render_all(sess))
    return 0


def sort_sessions(sess, key):
    """按指定字段排序"""
    if key == "cpu":
        sess.sort(key=lambda r: -(r.get("cpu_pct") or 0))
    elif key == "mem":
        sess.sort(key=lambda r: -(r.get("mem_mb") or 0))
    elif key == "ctx":
        sess.sort(key=lambda r: (r.get("context_pct") is None,
                                 r.get("context_pct") or 0))
    elif key == "cost":
        sess.sort(key=lambda r: -(r.get("cost") or 0))
    elif key == "tokens":
        sess.sort(key=lambda r: -((r.get("input_tokens") or 0) +
                                   (r.get("output_tokens") or 0)))
    return sess


def cmd_panel(args):
    if sys.stdin.isatty() and sys.stdout.isatty():
        from .panel import run
        return run(args)
    return refresh_panel(args)


def refresh_panel(args):
    """交互式面板"""
    from .notify import Notifier
    notifier = Notifier()
    interval = args.refresh
    try:
        sys.stdout.write(render.HIDE_CURSOR)
        while True:
            sess = agents_mod.collect(context_limit=args.context_limit)
            node_rows = nodeheap.collect() if args.node else None
            totals = agents_mod.totals(sess)
            al = agents_mod.alerts(sess)
            notifier.send(al)

            w = render.term_width()
            sys.stdout.write(render.CLEAR)
            sys.stdout.write(f"{BOLD}agentscope{RESET} "
                             f"{GRAY}— AI Agent 会话监控{RESET}\n\n")

            sl = system_load()
            if sl:
                sys.stdout.write(render.load_line(sl) + "\n\n")

            if node_rows is not None:
                sys.stdout.write(render.node_section(node_rows, w) + "\n\n")

            sys.stdout.write(render.session_table(sess, w) + "\n\n")
            sys.stdout.write(render.summary_line(totals) + "\n")

            if al:
                sys.stdout.write("\n" + render.alerts_section(al) + "\n")

            issues = nodeheap.diagnose(node_rows) if node_rows else []
            if issues:
                sys.stdout.write("\n" + render.alerts_section([
                    {"level": i["level"], "kind": i["kind"], "msg": i["msg"]}
                    for i in issues]) + "\n")

            sys.stdout.write(f"{GRAY}刷新间隔 {interval}s · Ctrl-C 退出{RESET}")
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(render.SHOW_CURSOR + RESET + "\n")
        sys.stdout.flush()
    return 0


def cmd_node(args):
    """node 内存与堆上限诊断"""
    def render_node(rows):
        parts = [render.node_section(rows), ""]
        issues = nodeheap.diagnose(rows)
        if issues:
            parts.append(BOLD + "诊断" + RESET)
            for i in issues:
                color = RED if i["level"] == "warn" else YELLOW
                parts.append(f"  {color}*{RESET} {i['kind']:<22} {i['msg']}")
                if i.get("suggest"):
                    parts.append(f"      → {i['suggest']}")
            parts.append("")
        else:
            parts.append(f"{GRAY}  未发现堆上限或并行实例问题{RESET}\n")
        return "\n".join(parts)

    if getattr(args, "watch", False):
        def collect():
            return nodeheap.collect()
        return watch_mod.run_watch(
            collect, render_node,
            interval=getattr(args, "interval", 3),
            clear=not getattr(args, "no_clear", False),
            iterations=getattr(args, "iterations", 0))

    rows = nodeheap.collect()
    if args.json:
        print(json.dumps({
            "summary": nodeheap.summary(rows),
            "processes": rows,
            "groups": nodeheap.group_by_worktree(rows),
            "issues": nodeheap.diagnose(rows),
        }, ensure_ascii=False, indent=2, default=str))
        return 0

    print()
    print(render_node(rows))
    return 0


def cmd_stats(args):
    """跨会话汇总"""
    sess = agents_mod.collect(with_resources=True)
    t = agents_mod.totals(sess)
    if args.json:
        print(json.dumps(t, ensure_ascii=False, indent=2, default=str))
        return 0

    print()
    print(f"{BOLD}跨会话汇总{RESET}\n")
    print(f"  会话数      {t['sessions']}")
    print(f"  输入 token  {sessions.format_tokens(t['input_tokens'])}")
    print(f"  输出 token  {sessions.format_tokens(t['output_tokens'])}")
    print(f"  缓存读token {sessions.format_tokens(t['cache_read_tokens'])}")
    print(f"  总成本      {sessions.format_cost(t['cost'], t.get('cost_estimated', False))}")
    print(f"  内存合计    {t['mem_mb'] / 1024:.2f} GB")
    print(f"  CPU 合计    {t['cpu_pct']:.0f}%")
    if t["by_type"]:
        print()
        print(f"  {'类型':<12}{'数量':>6}{'成本':>12}{'内存':>12}")
        for k, v in sorted(t["by_type"].items()):
            print(f"  {k:<12}{v['n']:>6}{sessions.format_cost(v['cost'], v.get('cost_estimated', False)):>12}"
                  f"{v['mem'] / 1024:>10.1f}G")
    print()
    return 0


def cmd_alerts(args):
    """只显示告警"""
    sess = agents_mod.collect()
    al = agents_mod.alerts(sess)
    node_rows = nodeheap.collect()
    for i in nodeheap.diagnose(node_rows):
        al.append({"level": i["level"], "kind": i["kind"], "msg": i["msg"]})
    if args.json:
        print(json.dumps(al, ensure_ascii=False, indent=2))
        return 0
    print()
    if not al:
        print(f"{GRAY}  没有需要关注的会话{RESET}\n")
        return 0
    print(render.alerts_section(al))
    print()
    return 0


def cmd_whoami(args):
    """检测当前终端处于哪个 Agent 会话"""
    me = os.getpid()
    # 沿父进程链找
    chain = []
    pid = me
    for _ in range(24):
        p = procstat.proc_parent(pid)
        if p <= 1:
            break
        chain.append(p)
        pid = p

    sess = agents_mod.collect(with_resources=False)
    found = None
    confidence = "none"

    for ancestor in chain:
        found = next((s for s in sess if ancestor in (s.get("pids") or [s["pid"]])), None)
        if found:
            confidence = "ppid"
            break

    if not found:
        # 用 cwd 猜
        cwd = procstat.load_cwds([me]).get(me, "")
        cands = [s for s in sess if s.get("cwd") and cwd and
                 s["cwd"].startswith(cwd)]
        if cands:
            found = cands[0]
            confidence = "cwd-guess"

    if args.json:
        print(json.dumps({"session": found, "match_confidence": confidence},
                         ensure_ascii=False, indent=2, default=str))
        return 0

    if not found:
        print(f"\n  {GRAY}当前终端不在任何 Agent 会话中{RESET}\n")
        return 1

    print()
    print(f"  {BOLD}当前会话{RESET}  "
          f"{GRAY}(匹配方式: {confidence}){RESET}\n")
    ctx = found.get("context_pct")
    print(f"    PID       {found['pid']}")
    print(f"    类型      {found['agent_type']}")
    print(f"    模型      {found.get('model') or '-'}")
    print(f"    上下文    {ctx if ctx is not None else '-'}% 剩余")
    print(f"    成本      {sessions.format_cost(found.get('cost'), found.get('cost_estimated', False))}")
    print(f"    分支      {found.get('git_branch') or '-'}")
    print(f"    目录      {found.get('cwd') or '-'}")
    print()
    return 0


def register(sub):
    """把子命令注册到主 CLI 的 subparsers"""
    p = sub.add_parser("agents", help="列出 Agent 会话（Claude/Codex/OpenCode/Devin）")
    p.add_argument("--json", action="store_true")
    p.add_argument("--alerts", action="store_true", help="同时显示告警")
    p.add_argument("--fast", action="store_true", help="跳过 CPU/内存采集（更快）")
    p.add_argument("--context-limit", type=int, default=None,
                   help="上下文窗口上限，用于计算剩余比例")
    p.add_argument("--sort", default="mem",
                   choices=["cpu", "mem", "ctx", "cost", "tokens"],
                   help="排序依据（默认按内存降序）")
    p.add_argument("--watch", action="store_true",
                   help="持续监控，按间隔刷新")
    p.add_argument("--interval", type=int, default=3,
                   help="--watch 的刷新间隔（秒）")
    p.add_argument("--no-clear", action="store_true",
                   help="--watch 时不清屏，逐块追加（适合管道）")
    p.add_argument("--iterations", type=int, default=0,
                   help="--watch 刷新多少轮后退出（0 = 无限）")
    p.set_defaults(func=cmd_agents)

    p = sub.add_parser("panel", help="交互式监控面板")
    p.add_argument("--refresh", type=int, default=3, help="刷新间隔秒数")
    p.add_argument("--node", action="store_true", default=True,
                   help="显示 node 内存段（默认开启）")
    p.add_argument("--no-node", dest="node", action="store_false")
    p.add_argument("--context-limit", type=int, default=None)
    p.set_defaults(func=cmd_panel)

    p = sub.add_parser("node", help="node/Electron 内存与堆上限诊断")
    p.add_argument("--json", action="store_true")
    p.add_argument("--watch", action="store_true",
                   help="持续监控，按间隔刷新")
    p.add_argument("--interval", type=int, default=3,
                   help="--watch 的刷新间隔（秒）")
    p.add_argument("--no-clear", action="store_true",
                   help="--watch 时不清屏，逐块追加（适合管道）")
    p.add_argument("--iterations", type=int, default=0,
                   help="--watch 刷新多少轮后退出（0 = 无限）")
    p.set_defaults(func=cmd_node)

    p = sub.add_parser("stats", help="跨会话汇总统计")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("alerts", help="只显示需要关注的会话")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_alerts)

    p = sub.add_parser("whoami", help="检测当前终端处于哪个 Agent 会话")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_whoami)
