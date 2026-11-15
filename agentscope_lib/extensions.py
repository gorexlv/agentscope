"""Additional local inspection and explicitly requested maintenance commands."""
import argparse
import json
import math
from . import agents, disk, doctor, heavy, mcp, metrics, ports


def output(data, as_json):
    # Structured text is also useful in non-JSON mode; never print credential values.
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_mcp(args):
    rows = agents.collect(with_resources=False)
    data = mcp.collect(session_rows=rows,with_resources=not args.fast)
    if args.json:
        return output(data,True)
    print(mcp.render(data))
    return 0


def cmd_ports(args):
    return output(ports.collect(session_rows=agents.collect(with_resources=False)),args.json)


def cmd_disk(args):
    if args.clean:
        ok = disk.clean(args.clean,yes=args.yes)
        print('cleaned' if ok else 'skipped or remaining')
        return 0 if ok else 1
    return output(disk.collect(),args.json)



def cmd_timeline(args):
    try:
        return output(metrics.timeline(args.since),args.json)
    except ValueError:
        import sys
        print('Invalid --since: use an ISO timestamp or duration such as 30m/2h/7d',file=sys.stderr)
        return 2

def cmd_worktrees(args):
    from . import worktrees
    if args.days is not None and (not math.isfinite(args.days) or args.days < 0):
        raise ValueError('--days must be finite and nonnegative')
    if not math.isfinite(args.budget) or args.budget <= 0:
        raise ValueError('--budget must be finite and positive')
    include = set(args.include.split(','))
    if not include <= worktrees.KINDS:
        raise ValueError('--include accepts worktrees,prune,branches,slim')
    data = worktrees.collect(days=args.days, online=args.online, fetch=args.fetch, budget=args.budget)
    if args.action == 'clean':
        data = worktrees.clean(data, yes=args.yes, include=include, days=args.days, online=args.online)
    output(data, args.json)
    return 1 if any(r['result'] == 'skipped' for r in data.get('outcomes', [])) else 0


def register(sub):
    p = sub.add_parser('worktrees', help='Bounded worktree inventory and explicit maintenance')
    p.add_argument('action', nargs='?', choices=['clean'])
    p.add_argument('--json', action='store_true')
    p.add_argument('--online', action='store_true', help='Look up PR state using gh')
    p.add_argument('--fetch', action='store_true', help='Explicitly refresh remote refs')
    p.add_argument('--budget', type=float, default=90, help='Inventory time budget in seconds (default: 90)')
    p.add_argument('--days', type=float, help='Idle worktree threshold (default: config or 30)')
    p.add_argument('--yes', action='store_true')
    p.add_argument('--include', default='worktrees,prune,branches,slim')
    p.set_defaults(func=cmd_worktrees)
    p = sub.add_parser('mcp',help='MCP instances grouped by server name')
    p.add_argument('--json',action='store_true')
    p.add_argument('--fast',action='store_true')
    p.set_defaults(func=cmd_mcp)
    p = sub.add_parser('ports',help='Listening ports, session ownership and abandoned workspaces')
    p.add_argument('--json',action='store_true')
    p.set_defaults(func=cmd_ports)
    p = sub.add_parser('disk',help='Generated artifact sizes; suggestions by default')
    p.add_argument('--json',action='store_true')
    p.add_argument('--clean',metavar='PATH')
    p.add_argument('--yes',action='store_true')
    p.set_defaults(func=cmd_disk)
    p = sub.add_parser('doctor',help='Offline credential presence, quota and hook checks')
    p.add_argument('--json',action='store_true')
    p.set_defaults(func=lambda args:output(doctor.collect(),args.json))
    p = sub.add_parser('timeline',help='Read rotated watch metrics')
    p.add_argument('--since',default='1h',help='ISO timestamp or duration: 30m/2h/7d')
    p.add_argument('--json',action='store_true')
    p.set_defaults(func=cmd_timeline)
    p = sub.add_parser('run',help='Run a command, optionally queue behind a host-wide heavy-task lock')
    p.add_argument('--heavy',action='store_true')
    p.add_argument('--max-load',type=float)
    p.add_argument('--wait-timeout',type=float,default=300)
    p.add_argument('--dry-run',action='store_true')
    p.add_argument('command',nargs=argparse.REMAINDER)
    p.set_defaults(func=heavy.run)
