"""Interactive terminal panel with explicit confirmation bound to a group identity."""
import os
import select
import sys
import termios
import time
import tty
from . import agents, cleanup, render, nodeheap
from .cli import sort_sessions
from .notify import Notifier, group_key


def read_key(timeout):
    if not select.select([sys.stdin], [], [], timeout)[0]:
        return ''
    key = os.read(sys.stdin.fileno(), 1).decode(errors='replace')
    if key == '\x1b':
        for _ in range(2):
            if select.select([sys.stdin], [], [], .05)[0]:
                key += os.read(sys.stdin.fileno(), 1).decode(errors='replace')
    return key


def tree_lines(group, info):
    members = set(group['members'])
    def depth(pid):
        seen = set()
        while pid in info and info[pid]['ppid'] in members and pid not in seen:
            seen.add(pid)
            pid = info[pid]['ppid']
        return len(seen)
    return ['  ' * (depth(p) + 1) + f"{p} {info[p]['comm']} (ppid {info[p]['ppid']})"
            for p in sorted(members, key=lambda p: (depth(p), p))]


def run(args):
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    selected = None
    expanded = False
    sort = 0
    keys = ['mem', 'cpu', 'ctx', 'cost', 'tokens']
    notifier = Notifier()
    pending = None
    message = ''
    deadline = 0
    groups = []
    info = {}
    sess = []
    node_rows = None
    try:
        tty.setraw(fd)
        sys.stdout.write(render.HIDE_CURSOR)
        while True:
            if time.monotonic() >= deadline:
                info, groups = cleanup.analyze()
                sess = agents.collect(context_limit=args.context_limit)
                notifier.send(agents.alerts(sess))
                node_rows = nodeheap.collect() if args.node else None
                if selected not in [group_key(g) for g in groups]:
                    selected = group_key(groups[0]) if groups else None
                    pending = None
                deadline = time.monotonic() + max(.1, args.refresh)
            sort_sessions(sess, keys[sort])
            lines = ['agentscope · 排序 ' + keys[sort], render.session_table(sess), render.summary_line(agents.totals(sess)), render.alerts_section(agents.alerts(sess))]
            if node_rows is not None:
                lines.append(render.node_section(node_rows))
            lines.extend(['', '进程组（选择清理目标）'])
            for g in groups:
                active = group_key(g) == selected
                status = '可清理' if cleanup.cleanable(g, info) else '保留'
                lines.append(('> ' if active else '  ') + f"pgid {g['pgid']} · {g['size']} 进程 · {status} · {g['cwd']}")
                if active and expanded:
                    lines.extend(tree_lines(g, info))
            lines.extend(['', message, '↑↓/jk 选择 · Enter 展开 · c 清理（y 确认） · s 排序 · q 退出'])
            sys.stdout.write(render.CLEAR + '\r\n'.join(line.replace('\n', '\r\n') for line in lines))
            sys.stdout.flush()
            key = read_key(max(0, deadline - time.monotonic()))
            if key in ('q', '\x03'):
                break
            if pending is not None:
                if key == 'y':
                    ok = cleanup.kill_group(pending['pgid'], mode='panel', expected=pending)
                    message = '已清理' if ok else '已跳过或仍有残留'
                    pending = None
                    deadline = 0
                elif key:
                    pending = None
                    message = '已取消'
                continue
            ids = [group_key(g) for g in groups]
            if key in ('j', 'k', '\x1b[A', '\x1b[B') and ids:
                idx = ids.index(selected)
                selected = ids[(idx + (1 if key in ('j', '\x1b[B') else -1)) % len(ids)]
            elif key in ('\r', '\n'):
                expanded = not expanded
            elif key == 's':
                sort = (sort + 1) % len(keys)
            elif key == 'c' and selected:
                g = next(g for g in groups if group_key(g) == selected)
                if cleanup.cleanable(g, info):
                    pending = g
                    message = f"确认清理 pgid {g['pgid']}？按 y 确认，其他键取消"
                else:
                    message = '该组受保护或不是可清理的孤儿组'
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write(render.SHOW_CURSOR + render.RESET + '\n')
        sys.stdout.flush()
    return 0
