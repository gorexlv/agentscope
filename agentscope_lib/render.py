"""
终端渲染：Agent 会话面板、表格、node 内存诊断表。

参考 ctop 的设计（单终端面板），但用纯 ANSI 实现，不引入第三方依赖。
"""

import os
import shutil
import sys
import time

from . import sessions

ESC = "\x1b"
CLEAR = f"{ESC}[2J{ESC}[H"
HOME = f"{ESC}[H"
CLR_LINE = f"{ESC}[K"
HIDE_CURSOR = f"{ESC}[?25l"
SHOW_CURSOR = f"{ESC}[?25h"
BOLD = f"{ESC}[1m"
DIM = f"{ESC}[2m"
RESET = f"{ESC}[0m"
RED = f"{ESC}[31m"
GREEN = f"{ESC}[32m"
YELLOW = f"{ESC}[33m"
BLUE = f"{ESC}[34m"
MAGENTA = f"{ESC}[35m"
CYAN = f"{ESC}[36m"
GRAY = f"{ESC}[90m"

AGENT_COLORS = {
    "claude": f"{ESC}[38;5;215m",     # 橙
    "codex": f"{ESC}[38;5;114m",      # 绿
    "opencode": f"{ESC}[38;5;114m",
    "devin": f"{ESC}[38;5;141m",      # 紫
}
DEFAULT_COLOR = CYAN


def term_width():
    try:
        return shutil.get_terminal_size((100, 30)).columns
    except Exception:
        return 100


def _visible_len(s):
    """去掉 ANSI 后的可见长度"""
    out = 0
    i = 0
    while i < len(s):
        if s[i] == ESC:
            j = s.find("m", i)
            if j == -1:
                break
            i = j + 1
            continue
        out += 1
        i += 1
    return out


def _pad(s, width):
    gap = width - _visible_len(s)
    return s + " " * max(0, gap)


def _strip_ansi(s):
    out = []
    i = 0
    while i < len(s):
        if s[i] == ESC:
            j = s.find("m", i)
            if j == -1:
                break
            i = j + 1
            continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _trunc(s, width):
    """截断到指定可见宽度，保留 ANSI 序列（颜色不丢）"""
    if _visible_len(s) <= width:
        return s
    if width <= 1:
        return "…"
    # 逐字符复制，ANSI 序列原样保留，可见字符数超限时追加省略号
    out = []
    visible = 0
    i = 0
    while i < len(s):
        if s[i] == ESC:
            j = s.find("m", i)
            if j == -1:
                break
            out.append(s[i:j + 1])
            i = j + 1
            continue
        if visible >= width - 1:
            break
        out.append(s[i])
        visible += 1
        i += 1
    out.append("…")
    # 若截断点落在 ANSI 序列中间，补上 RESET，避免颜色溢出到后续单元格
    res = "".join(out)
    if res.count(ESC) > res.count(RESET):
        res += RESET
    return res


def ctx_color(pct):
    """上下文剩余比例配色"""
    if pct is None:
        return GRAY
    if pct <= 10:
        return RED
    if pct <= 25:
        return YELLOW
    return GREEN


def mem_color(mb):
    if mb is None:
        return GRAY
    if mb >= 4096:
        return RED
    if mb >= 2048:
        return YELLOW
    return ""


def cpu_color(pct):
    if pct is None:
        return GRAY
    if pct >= 80:
        return RED
    if pct >= 40:
        return YELLOW
    return ""


def short_cwd(cwd, maxlen=34):
    if not cwd:
        return GRAY + "-" + RESET
    home = os.path.expanduser("~")
    c = cwd.replace(home, "~")
    if len(c) > maxlen:
        # 保留尾部（更有辨识度）
        c = "…" + c[-(maxlen - 1):]
    return c


def render_table(sess_list, show_node=False, node_rows=None, width=None):
    """渲染会话表格（文本模式）"""
    w = width or term_width()
    lines = []

    if show_node and node_rows:
        lines.append(node_section(node_rows, w))
        lines.append("")

    lines.append(session_table(sess_list, w))
    return "\n".join(lines)


def session_table(sess_list, w=None):
    """会话明细表"""
    w = w or term_width()
    out = []
    # CPU% 是「单核占用率」（100% = 吃满一个核），不是整机占比
    header = (f"{'  PID':>7} {'AGENT':<9} {'CPU*':>6} {'MEM':>9} {'CTX':>5} "
              f"{'TOKENS':>9} {'COST':>8} {'MODEL':<22} {'BRANCH':<18} CWD")
    out.append(f"{BOLD}{header}{RESET}")
    out.append(f"{GRAY}{'─' * min(len(header), w)}{RESET}")

    if not sess_list:
        out.append(f"{GRAY}  没有正在运行的 Agent 会话{RESET}")
        return "\n".join(out)

    for s in sess_list:
        color = AGENT_COLORS.get(s["agent_type"], DEFAULT_COLOR)
        cpu = s.get("cpu_pct") or 0
        mem = s.get("mem_mb")
        ctx = s.get("context_pct")
        tokens = ((s.get("input_tokens") or 0) + (s.get("output_tokens") or 0))
        cost = s.get("cost")

        # 先格式化纯文本，再套颜色 —— 否则格式说明符会作用在 ANSI 序列上
        cpu_s = f"{cpu:.0f}%"
        mem_s = f"{mem:.0f}M" if mem else "-"
        ctx_s = f"{ctx}%" if ctx is not None else "-"

        # 单元格 = 颜色 + 文本 + 填充，颜色只包裹文本，填充留在颜色之外，
        # 这样可见宽度 = len(文本) + len(填充)，终端渲染不会错位。
        def cell(text, width, c=""):
            if len(text) > width:
                text = text[:width]
            pad = " " * (width - len(text))
            return f"{c}{text}{RESET}{pad}" if c else f"{text}{pad}"

        # 主进程与子进程用不同标记区分（子进程不重复计费）
        # 用 ASCII 字符：●/· 是多字节，按字符数算宽度会错位
        mark = "*" if s.get("is_main", True) else " "
        row = (
            f"{mark}{s['pid']:>6} "
            f"{cell(s['agent_type'], 9, color)} "
            f"{cell(cpu_s, 5, cpu_color(cpu))} "
            f"{cell(mem_s, 9, mem_color(mem))} "
            f"{cell(ctx_s, 5, ctx_color(ctx))} "
            f"{cell(sessions.format_tokens(tokens), 9)} "
            f"{cell(sessions.format_cost(cost), 8)} "
            f"{cell(_trunc(s.get('model') or '-', 22), 22)} "
            f"{cell(_trunc(s.get('git_branch') or '-', 18), 18)} "
            f"{GRAY}{short_cwd(s.get('cwd'), 36)}{RESET}"
        )
        out.append(row)

    # 脚注：解释 CPU% 的含义与合计
    total_cpu = sum((s.get("cpu_pct") or 0) for s in sess_list)
    main_cpu = sum((s.get("cpu_pct") or 0) for s in sess_list
                   if s.get("is_main", True))
    out.append(f"{GRAY}  CPU* 为单核占用率（100% = 吃满一核）· "
               f"会话合计 {total_cpu:.0f}% "
               f"（主进程 {main_cpu:.0f}%）{RESET}")
    return "\n".join(out)


def node_section(rows, w=None):
    """node 内存诊断段"""
    w = w or term_width()
    out = []
    out.append(f"{BOLD}node / Electron 内存占用{RESET}")

    if not rows:
        out.append(f"{GRAY}  没有 node 类进程{RESET}")
        return "\n".join(out)

    total = sum(r["mem_mb"] or 0 for r in rows)
    with_heap = [r for r in rows if r["heap_limit_mb"]]
    max_heap = max((r["heap_limit_mb"] for r in with_heap), default=None)
    out.append(f"{GRAY}  {len(rows)} 个进程 · 合计 {total / 1024:.2f} GB"
               f" · 配置了堆上限的 {len(with_heap)} 个"
               + (f" · 最大 {max_heap} MB" if max_heap else "")
               + f"{RESET}")
    out.append("")

    hdr = (f"  {'PID':>7} {'MEM':>9} {'HEAP':>9} {'ROLE':<17} WORKTREE")
    out.append(f"{BOLD}{hdr}{RESET}")
    out.append(f"{GRAY}  {'─' * (len(hdr) - 2)}{RESET}")

    for r in rows[:14]:
        mem = r["mem_mb"]
        lim = r["heap_limit_mb"]
        mem_s = f"{mem:.0f}M" if mem else "-"
        heap_s = f"{lim}MB" if lim else "-"
        hcolor = RED if (lim and lim >= 4096) else ""
        def ncell(text, width, c=""):
            if len(text) > width:
                text = text[:width]
            pad = " " * (width - len(text))
            return f"{c}{text}{RESET}{pad}" if c else f"{text}{pad}"

        out.append(
            f"  {r['pid']:>7} "
            f"{ncell(mem_s, 9, mem_color(mem))} "
            f"{ncell(heap_s, 9, hcolor)} "
            f"{ncell(_trunc(r['role'], 17), 17)} "
            f"{GRAY}{_trunc(r['worktree'] or '-', 40)}{RESET}"
        )
    if len(rows) > 14:
        out.append(f"{GRAY}  ...另 {len(rows) - 14} 个{RESET}")
    return "\n".join(out)


def load_line(sl):
    """系统负载摘要行 —— 与会话视图对照"""
    if not sl:
        return ""
    l1 = sl.get("load_1m", 0)
    cores = sl.get("cores", 1)
    busy = sl.get("busy_pct", 0)
    color = RED if busy >= 80 else (YELLOW if busy >= 50 else GREEN)
    bar_n = int(busy / 5)
    bar = "█" * bar_n + "░" * (20 - bar_n)
    return (
        f"{BOLD}系统负载{RESET}  "
        f"{color}{bar} {busy:.0f}%{RESET}  "
        f"{GRAY}load{RESET} {l1:.2f} "
        f"{GRAY}(1m) / {sl.get('load_5m', 0):.2f} (5m) / "
        f"{sl.get('load_15m', 0):.2f} (15m){RESET}  "
        f"{GRAY}· {cores} 核{RESET}"
    )


def summary_line(t):
    """汇总行"""
    parts = [
        f"{BOLD}{t['sessions']} 个会话{RESET}",
        f"token {sessions.format_tokens(t['input_tokens'] + t['output_tokens'])}",
        f"成本 {sessions.format_cost(t['cost'])}",
        f"内存 {t['mem_mb'] / 1024:.2f} GB",
        f"CPU {t['cpu_pct']:.0f}%",
    ]
    bt = []
    for k, v in sorted(t["by_type"].items()):
        bt.append(f"{k}:{v['n']}")
    if bt:
        parts.append(" ".join(bt))
    return f"{GRAY} │ {RESET}".join(parts)


def alerts_section(alerts):
    if not alerts:
        return ""
    out = [f"{BOLD}告警{RESET}"]
    for a in alerts:
        color = RED if a["level"] == "warn" else YELLOW
        out.append(f"  {color}●{RESET} {a['kind']:<20} {a['msg']}")
    return "\n".join(out)


# ── 交互式面板 ────────────────────────────────────────────────

def panel(sess_list, node_rows=None, totals=None, alerts=None, refresh=3):
    """单终端面板 —— 类似 ctop 的视图，Ctrl-C 退出"""
    try:
        sys.stdout.write(HIDE_CURSOR)
        while True:
            w = term_width()
            sys.stdout.write(CLEAR)
            sys.stdout.write(f"{BOLD}agentscope{RESET} {GRAY}— AI Agent 会话监控{RESET}\n\n")

            if node_rows is not None:
                sys.stdout.write(node_section(node_rows, w) + "\n\n")

            sys.stdout.write(session_table(sess_list, w) + "\n\n")

            if totals:
                sys.stdout.write(summary_line(totals) + "\n")

            if alerts:
                sys.stdout.write("\n" + alerts_section(alerts) + "\n")

            sys.stdout.write(f"{GRAY}刷新间隔 {refresh}s · Ctrl-C 退出{RESET}")
            sys.stdout.flush()
            time.sleep(refresh)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(SHOW_CURSOR + RESET + "\n")
        sys.stdout.flush()
