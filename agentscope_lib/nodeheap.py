"""
node / Electron 进程的堆上限与内存诊断。

要解决的问题：
  AI 编程时会大量启用 worktree 并行启动，或同时编译 TS/JS。
  如果 node 的堆上限设得太大（--max-old-space-size=8192 之类），
  多个实例并行时内存占用会非常高，最终拖垮机器。

本模块做三件事：
  1. 找出所有 node / Electron 进程，读出它们的 --max-old-space-size 配置
  2. 用 footprint 测出每个进程的真实物理内存占用
  3. 按项目 / worktree 聚合，识别出「并行实例过多」和「堆上限过大」两类问题

命令行来源：pgrep -lf（受限环境下 ps 被禁，但 pgrep 可用）
内存来源：footprint -p <pid>（macOS 自带，精确的物理占用）
"""

import collections
import os
import re
import subprocess

from . import procstat

# 堆上限参数
HEAP_FLAGS = ("--max-old-space-size", "--max-semi-space-size",
              "--max-heap-size", "--max-old-space")

# node / Electron 类进程的可执行名
NODE_PROCS = ("node", "electron", "Electron", "Helper (Renderer)",
              "Helper (Plugin)", "esbuild", "tsx", "tsserver")

# 建议的堆上限（MB）—— 依据经验值
HEAP_SUGGESTION = {
    "node": 2048,
    "electron": 512,
    "esbuild": 512,
}

# 超过这个值就认为是「堆上限过大」
HEAP_WARN_MB = 4096


def _sh(args, timeout=30):
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:
        return ""


def node_commandlines():
    """所有 node 类进程的命令行 -> [(pid, cmdline)]"""
    out = _sh(["pgrep", "-lf", "node|Electron|esbuild|tsserver"])
    result = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        cmd = parts[1]
        # 排除自身与明显无关的
        if "pgrep" in cmd or "agentscope" in cmd:
            continue
        result.append((pid, cmd))
    return result


def parse_heap_limit(cmd):
    """从命令行解析堆上限（MB），返回 (limit_mb, flag_name) 或 (None, None)"""
    for flag in HEAP_FLAGS:
        m = re.search(re.escape(flag) + r"[= ](\d+)", cmd)
        if m:
            val = int(m.group(1))
            # --max-old-space-size 的单位是 MB
            return val, flag
    return None, None


def is_node_like(cmd, comm):
    """判断是否 node 类进程"""
    if "node" in comm.lower():
        return True
    for p in NODE_PROCS:
        if p in comm:
            return True
    # 命令行里出现 node 可执行路径
    if re.search(r"(^|/)node(\s|$)", cmd):
        return True
    if "electron" in cmd.lower():
        return True
    if "esbuild" in cmd.lower():
        return True
    return False


def classify(cmd):
    """进程角色：runtime / electron-renderer / build-tool / mcp-server"""
    low = cmd.lower()
    if "--type=renderer" in low or "helper (renderer)" in low:
        return "electron-renderer"
    if "--type=" in low:
        return "electron-helper"
    if "mcp-server" in low or "mcp_server" in low:
        return "mcp-server"
    if "esbuild" in low:
        return "build-tool"
    if "vite" in low:
        return "dev-server"
    if "tsserver" in low or "typescript" in low:
        return "language-server"
    if "electron" in low:
        return "electron-main"
    return "runtime"


def worktree_of(cwd):
    """从 cwd 推断 worktree 归属。

    worktree 通常长这样：
      ~/orca/workspaces/mo-/issue-143-datum-oidc
      ~/Workspace/github.com/shimohq/mo--fix-lark
    同一仓库的多个 worktree 会共享一个前缀，用它做聚合键。
    """
    if not cwd:
        return ""
    home = os.path.expanduser("~")
    if cwd.startswith(home):
        cwd = "~" + cwd[len(home):]
    parts = cwd.split("/")
    # 找 "-x" 形式的仓库段，或 workspaces/<owner>/<repo>
    for i, p in enumerate(parts):
        if p.startswith("-") and len(p) > 2:
            # 形如 mo--fix-lark 或 -Users-rexlv-orca-workspaces-mo-
            return "/".join(parts[:i + 1])
    if len(parts) >= 4 and parts[2] == "workspaces":
        return "/".join(parts[:4])
    return "/".join(parts[:3]) if len(parts) >= 3 else cwd


def collect(procs=None, with_memory=True):
    """采集 node 类进程的内存与堆配置"""
    if procs is None:
        procs = procstat.scan_processes()
    cwds = procstat.load_cwds(list(procs.keys()))

    candidates = node_commandlines()
    pids = [pid for pid, _ in candidates]

    mem_map = {}
    if with_memory and pids:
        mem_map = procstat.footprint_many(pids)

    rows = []
    for pid, cmd in candidates:
        b = procs.get(pid, {})
        comm = b.get("comm", "")
        if not is_node_like(cmd, comm):
            continue
        limit_mb, flag = parse_heap_limit(cmd)
        role = classify(cmd)
        cwd = cwds.get(pid, "")
        rows.append({
            "pid": pid,
            "comm": comm,
            "role": role,
            "cwd": cwd,
            "worktree": worktree_of(cwd),
            "heap_limit_mb": limit_mb,
            "heap_flag": flag,
            "mem_mb": round(mem_map[pid], 1) if pid in mem_map else None,
            "cmd": cmd,
        })

    rows.sort(key=lambda r: -(r["mem_mb"] or 0))
    return rows


def group_by_worktree(rows):
    """按 worktree 聚合"""
    agg = collections.defaultdict(lambda: {
        "count": 0, "mem_mb": 0.0, "heap_limits": [], "roles": collections.Counter(),
        "pids": [],
    })
    for r in rows:
        key = r["worktree"] or "(未知)"
        g = agg[key]
        g["count"] += 1
        g["mem_mb"] += r["mem_mb"] or 0
        g["pids"].append(r["pid"])
        g["roles"][r["role"]] += 1
        if r["heap_limit_mb"]:
            g["heap_limits"].append(r["heap_limit_mb"])
    out = []
    for k, g in agg.items():
        out.append({
            "worktree": k,
            "count": g["count"],
            "mem_mb": round(g["mem_mb"], 1),
            "max_heap_mb": max(g["heap_limits"]) if g["heap_limits"] else None,
            "roles": dict(g["roles"]),
            "pids": g["pids"],
        })
    out.sort(key=lambda x: -x["mem_mb"])
    return out


def diagnose(rows, parallel_warn=6, mem_warn_mb=4096):
    """生成诊断结论"""
    issues = []
    groups = group_by_worktree(rows)

    # 1. 堆上限过大
    for r in rows:
        if r["heap_limit_mb"] and r["heap_limit_mb"] >= HEAP_WARN_MB:
            issues.append({
                "level": "warn",
                "kind": "HEAP_LIMIT_HIGH",
                "pid": r["pid"],
                "msg": f"堆上限 {r['heap_limit_mb']} MB 偏大"
                       f"（{r['heap_flag']}），实际占用 {r['mem_mb'] or 0:.0f} MB",
                "cwd": r["cwd"],
                "suggest": f"建议降到 {HEAP_SUGGESTION.get('node', 2048)} MB"
                           f"（NODE_OPTIONS=--max-old-space-size={HEAP_SUGGESTION.get('node', 2048)}）",
            })

    # 2. 同一 worktree 并行实例过多
    for g in groups:
        if g["count"] >= parallel_warn:
            issues.append({
                "level": "warn",
                "kind": "TOO_MANY_INSTANCES",
                "pid": None,
                "msg": f"{g['worktree']} 下有 {g['count']} 个 node 实例，"
                       f"合计 {g['mem_mb']:.0f} MB",
                "cwd": g["worktree"],
                "suggest": "关闭不再需要的 dev server，或用 agentscope clean 回收残留",
            })

    # 3. 总量过大
    total = sum(r["mem_mb"] or 0 for r in rows)
    if total >= mem_warn_mb:
        issues.append({
            "level": "warn",
            "kind": "TOTAL_MEM_HIGH",
            "pid": None,
            "msg": f"node 类进程总占用 {total / 1024:.1f} GB",
            "cwd": "",
            "suggest": "考虑限制并行 worktree 数量，或降低堆上限",
        })

    return issues


def summary(rows):
    """汇总统计"""
    total_mem = sum(r["mem_mb"] or 0 for r in rows)
    with_heap = [r for r in rows if r["heap_limit_mb"]]
    roles = collections.Counter(r["role"] for r in rows)
    return {
        "processes": len(rows),
        "total_mem_mb": round(total_mem, 1),
        "total_mem_gb": round(total_mem / 1024, 2),
        "with_heap_limit": len(with_heap),
        "max_heap_mb": max((r["heap_limit_mb"] for r in with_heap), default=None),
        "roles": dict(roles),
    }
