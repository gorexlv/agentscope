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


def cmd_agents(args):
    """列出 Agent 会话"""
    sess = agents_mod.collect(context_limit=args.context_limit,
                              with_resources=not args.fast)
    if args.json:
        print(json.dumps(sess, ensure_ascii=False, indent=2, default=str))
        return 0

    print()
    print(render.session_table(sess))
    print()
    t = agents_mod.totals(sess)
    print(render.summary_line(t))
    if args.alerts:
        al = agents_mod.alerts(sess)
        if al:
            print()
            print(render.alerts_section(al))
    return 0


def cmd_panel(args):
    """交互式面板"""
    interval = args.refresh
    try:
        sys.stdout.write(render.HIDE_CURSOR)
        while True:
            sess = agents_mod.collect(context_limit=args.context_limit)
            node_rows = nodeheap.collect() if args.node else None
            totals = agents_mod.totals(sess)
            al = agents_mod.alerts(sess)

            w = render.term_width()
            sys.stdout.write(render.CLEAR)
            sys.stdout.write(f"{render.BOLD}agentscope{RESET} "
                             f"{render.GRAY}— AI Agent 会话监控{RESET}\n\n")

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

            sys.stdout.write(f"{render.GRAY}刷新间隔 {interval}s · Ctrl-C 退出{RESET}")
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(render.SHOW_CURSOR + render.RESET + "\n")
        sys.stdout.flush()
    return 0


def cmd_node(args):
    """node 内存与堆上限诊断"""
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
    print(render.node_section(rows))
    print()
    issues = nodeheap.diagnose(rows)
    if issues:
        print(render.BOLD + "诊断" + render.RESET)
        for i in issues:
            color = render.RED if i["level"] == "warn" else render.YELLOW
            print(f"  {color}●{render.RESET} {i['kind']:<22} {i['msg']}")
            if i.get("suggest"):
                print(f"      → {i['suggest']}")
        print()
    else:
        print(f"{render.GRAY}  未发现堆上限或并行实例问题{render.RESET}\n")
    return 0


def cmd_stats(args):
    """跨会话汇总"""
    sess = agents_mod.collect(with_resources=True)
    t = agents_mod.totals(sess)
    if args.json:
        print(json.dumps(t, ensure_ascii=False, indent=2, default=str))
        return 0

    print()
    print(f"{render.BOLD}跨会话汇总{render.RESET}\n")
    print(f"  会话数      {t['sessions']}")
    print(f"  输入 token  {sessions.format_tokens(t['input_tokens'])}")
    print(f"  输出 token  {sessions.format_tokens(t['output_tokens'])}")
    print(f"  缓存读token {sessions.format_tokens(t['cache_read_tokens'])}")
    print(f"  总成本      {sessions.format_cost(t['cost'])}")
    print(f"  内存合计    {t['mem_mb'] / 1024:.2f} GB")
    print(f"  CPU 合计    {t['cpu_pct']:.0f}%")
    if t["by_type"]:
        print()
        print(f"  {'类型':<12}{'数量':>6}{'成本':>12}{'内存':>12}")
        for k, v in sorted(t["by_type"].items()):
            print(f"  {k:<12}{v['n']:>6}{sessions.format_cost(v['cost']):>12}"
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
        print(f"{render.GRAY}  没有需要关注的会话{render.RESET}\n")
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

    for s in sess:
        if s["pid"] in chain:
            found = s
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
        print(f"\n  {render.GRAY}当前终端不在任何 Agent 会话中{render.RESET}\n")
        return 1

    print()
    print(f"  {render.BOLD}当前会话{render.RESET}  "
          f"{render.GRAY}(匹配方式: {confidence}){render.RESET}\n")
    ctx = found.get("context_pct")
    print(f"    PID       {found['pid']}")
    print(f"    类型      {found['agent_type']}")
    print(f"    模型      {found.get('model') or '-'}")
    print(f"    上下文    {ctx if ctx is not None else '-'}% 剩余")
    print(f"    成本      {sessions.format_cost(found.get('cost'))}")
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
    p.add_argument("--context-limit", type=int, default=200000,
                   help="上下文窗口上限，用于计算剩余比例")
    p.set_defaults(func=cmd_agents)

    p = sub.add_parser("panel", help="交互式监控面板")
    p.add_argument("--refresh", type=int, default=3, help="刷新间隔秒数")
    p.add_argument("--node", action="store_true", default=True,
                   help="显示 node 内存段（默认开启）")
    p.add_argument("--no-node", dest="node", action="store_false")
    p.add_argument("--context-limit", type=int, default=200000)
    p.set_defaults(func=cmd_panel)

    p = sub.add_parser("node", help="node/Electron 内存与堆上限诊断")
    p.add_argument("--json", action="store_true")
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
