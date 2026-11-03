"""
Agent 会话聚合：把进程与会话文件关联，产出统一视图。

支持四类 Agent：
  claude   Claude Code      ~/.claude/projects/<slug>/<id>.jsonl
  codex    Codex CLI         ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
  opencode OpenCode          ~/.local/share/opencode/storage/session/**
  devin    Devin             终端会话，尽力识别

关联策略（按可靠度排序）：
  1. 进程 cwd == 会话 cwd                → 最可靠
  2. 进程可执行路径含 Agent 特征 + 同项目 → 次之
  3. 仅时间最近 + 同项目                  → 兜底
"""

import os
import re
import time

from . import procstat
from . import sessions

AGENT_TYPES = ("claude", "codex", "opencode", "devin")

# 进程可执行路径 / 命令名 → Agent 类型
AGENT_PATTERNS = [
    # Claude Code
    ("claude-code", "claude"),
    ("/claude.app/Contents/MacOS/claude", "claude"),
    ("claude-code/", "claude"),
    # Codex
    ("codex-cli", "codex"),
    ("/codex", "codex"),
    ("codex", "codex"),
    # OpenCode
    ("opencode", "opencode"),
    # Devin
    ("devin", "devin"),
]

# 明确不是 Agent 的进程（避免 "codex" 匹配到无关东西）
NEGATIVE = ("claude-manager", "agentscope", "codex-code-mode-host",
            "codex/app-server", "codex-cli-wrapper")


def detect_agent_type(path, comm):
    """从进程路径/名判断 Agent 类型"""
    hay = f"{path} {comm}".lower()
    for neg in NEGATIVE:
        if neg in hay:
            return None
    # 更具体的 claude 特征优先
    if "claude-code" in hay or "/claude.app/" in hay:
        return "claude"
    if "codex-cli" in hay or "/codex" in hay:
        return "codex"
    if "opencode" in hay:
        return "opencode"
    if "devin" in hay:
        return "devin"
    return None


def _norm(p):
    return os.path.normpath(p) if p else ""


def build_index(home=None, cwds=None, only_cwds=None):
    """构建会话索引。

    性能要点：会话文件有几百个，逐个解析要 11 秒以上。
    优化路径：Claude Code 的项目目录名就是 cwd 的 slug（'/' 和 '.' 换成 '-'），
    因此可以**不读文件内容**就定位到候选目录，只解析真正需要的几个文件。

    only_cwds: 只为这些 cwd 解析会话内容（None = 全量解析，供离线分析）。
    """
    home = home or os.path.expanduser("~")
    idx = {"claude": [], "codex": [], "opencode": [], "devin": []}

    # ── Claude Code：用 slug 直接定位 ──
    claude_root = os.path.join(home, ".claude", "projects")
    wanted_slugs = set()
    if only_cwds:
        for c in only_cwds:
            if c:
                wanted_slugs.add(sessions.cwd_to_slug(c))

    if os.path.isdir(claude_root):
        for proj in os.listdir(claude_root):
            pdir = os.path.join(claude_root, proj)
            if not os.path.isdir(pdir):
                continue
            # 只处理需要的目录（无过滤时全要）
            if wanted_slugs and proj not in wanted_slugs:
                continue
            try:
                names = os.listdir(pdir)
            except OSError:
                continue
            for name in names:
                if not name.endswith(".jsonl"):
                    continue
                fp = os.path.join(pdir, name)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                idx["claude"].append({
                    "path": fp, "session_id": name[:-6],
                    "agent_type": "claude", "project": proj,
                    "mtime": st.st_mtime, "size": st.st_size,
                    "cwd": None, "data": None,
                })
    idx["claude"].sort(key=lambda x: -x["mtime"])

    # ── Codex：目录结构是日期，需要遍历但文件数远少于 Claude ──
    for f in sessions.codex_session_files(home):
        f["agent_type"] = "codex"
        f["cwd"] = None
        f["data"] = None
        idx["codex"].append(f)

    for f in sessions.opencode_session_files(home):
        f["agent_type"] = "opencode"
        f["cwd"] = None
        f["data"] = None
        idx["opencode"].append(f)

    return idx


_PARSERS = {
    "claude": lambda p: sessions.parse_claude_session(p),
    "codex": lambda p: sessions.parse_codex_session(p),
    "opencode": lambda p: sessions.parse_opencode_session(p),
}


def _ensure_parsed(agent_type, f):
    """按需解析单个会话文件（用于 lazy 候选）"""
    if f.get("data") is None:
        parser = _PARSERS.get(agent_type)
        if parser:
            f["data"] = parser(f["path"])
            f["cwd"] = (f["data"] or {}).get("cwd")
            f["session_id"] = (f["data"] or {}).get("session_id") or f.get("session_id")
    return f.get("data") or {}


def _match_session(agent_type, cwd, idx, max_age=7200):
    """给一个进程找对应的会话文件。

    Claude Code 有捷径：项目目录名就是 cwd 的 slug，可以直接定位，
    不需要读文件内容。
    """
    cands = idx.get(agent_type) or []
    if not cands:
        return None
    ncwd = _norm(cwd)
    now = time.time()

    # ── 捷径：Claude Code 用 slug 精确命中 ──
    if agent_type == "claude" and cwd:
        slug = sessions.cwd_to_slug(cwd)
        same = [f for f in cands if f.get("project") == slug]
        if same:
            same.sort(key=lambda x: -x["mtime"])
            # 同目录可能有多个会话文件（不同 session），取最近的
            return same[0]

    def score(f):
        """匹配度：cwd 完全匹配 > 父子关系 > 未匹配"""
        sc = f.get("cwd")
        if sc and _norm(sc) == ncwd:
            return 3
        if sc and ncwd and (_norm(sc).startswith(ncwd + os.sep) or
                            ncwd.startswith(_norm(sc) + os.sep)):
            return 2
        return 0

    # 1. 已解析的候选里找最佳匹配
    parsed = [f for f in cands if f.get("data") is not None]
    best = None
    best_score = 0
    for f in parsed:
        sc = score(f)
        if sc > best_score:
            best, best_score = f, sc
    if best_score >= 2:
        return best

    # 2. 未解析的候选 —— 逐个按需解析，找到匹配的即停
    lazy = sorted([f for f in cands if f.get("data") is None],
                  key=lambda x: -x["mtime"])
    for f in lazy:
        if now - f["mtime"] > max_age:
            break
        _ensure_parsed(agent_type, f)
        if score(f) >= 2:
            return f

    # 3. 兜底：最近的已解析候选
    if best is not None:
        return best
    fresh = [f for f in parsed if now - f["mtime"] < max_age]
    if fresh:
        fresh.sort(key=lambda x: -x["mtime"])
        return fresh[0]
    return None


def collect(home=None, with_resources=True, context_limit=None):
    """采集所有 Agent 会话。

    返回 session 列表，每项包含：
      pid / agent_type / cwd / model / cpu_pct / mem_mb /
      input_tokens / output_tokens / context_pct / context_used /
      context_limit / cost / git_branch / title / session_id
    """
    home = home or os.path.expanduser("~")
    procs = procstat.scan_processes()
    cwds = procstat.load_cwds(list(procs.keys()))

    # 先筛出 Agent 进程
    agent_list = []
    for pid, b in procs.items():
        at = detect_agent_type(b.get("path", ""), b.get("comm", ""))
        if not at:
            continue
        agent_list.append((pid, at, b))

    # 用 Agent 进程的 cwd 集合来限定索引范围 —— 只解析相关目录下的会话文件
    agent_cwds = {cwds.get(pid, "") for pid, _, _ in agent_list}
    agent_cwds.discard("")
    idx = build_index(home, cwds, only_cwds=agent_cwds or None)

    # CPU 采样（两次快照做差）
    cpu_map = {}
    if with_resources and agent_list:
        snap = procstat.cpu_snapshot([p for p, _, _ in agent_list])
        cpu_map = procstat.cpu_percent(snap)

    # 内存（footprint 逐个调用）
    mem_map = {}
    if with_resources and agent_list:
        mem_map = procstat.footprint_many([p for p, _, _ in agent_list])

    out = []
    for pid, at, b in agent_list:
        cwd = cwds.get(pid, "")
        sess = _match_session(at, cwd, idx)
        rec = {
            "pid": pid,
            "agent_type": at,
            "pgid": b.get("pgid"),
            "ppid": b.get("ppid"),
            "cwd": cwd,
            "executable": b.get("path", ""),
            "comm": b.get("comm", ""),
            "cpu_pct": round(cpu_map.get(pid, 0.0), 1),
            "mem_mb": round(mem_map.get(pid, 0.0), 1) if pid in mem_map else None,
            "model": None, "input_tokens": None, "output_tokens": None,
            "cache_read_tokens": None, "cache_create_tokens": None,
            "context_used": None, "context_limit": None, "context_pct": None,
            "cost": None, "git_branch": None, "title": None,
            "session_id": None, "session_path": None,
            "last_turn_ms": None, "timestamp": None,
            "is_main": False,
        }

        if sess:
            rec["session_path"] = sess["path"]
            rec["session_id"] = sess.get("session_id")
            # 按需解析（索引阶段多数文件是懒加载的）
            data = sess.get("data")
            if not data:
                data = _ensure_parsed(at, sess)
            # 只有会话主进程承载 token / 成本，避免同一会话被重复计费
            rec["is_main"] = _is_session_main(b)
            if rec["is_main"]:
                rec.update(data)
                rec["cost"] = sessions.calculate_cost(
                    rec["model"], rec["input_tokens"], rec["output_tokens"],
                    rec["cache_create_tokens"], rec["cache_read_tokens"], at)
            else:
                # 子进程仍显示模型与分支（便于识别），但不重复计入成本
                rec["model"] = data.get("model")
                rec["git_branch"] = data.get("git_branch")
                rec["title"] = data.get("title")
            if not rec["cwd"] and data.get("cwd"):
                rec["cwd"] = data["cwd"]
        else:
            rec["status"] = "NO_SESSION"

        out.append(rec)

    # 主进程排前面，其次按内存降序
    out.sort(key=lambda r: (r["agent_type"], not r["is_main"],
                             -(r.get("mem_mb") or 0)))
    return out


def _is_session_main(b):
    """判断这个进程是否是会话的主进程。

    Claude Code / Codex 这类 Electron 应用会派生一批 Helper / Renderer 子进程，
    它们共享同一份会话文件。若每个进程都算一次成本，会被重复计费十几倍。

    判据：进程名里带 Helper / Renderer / disclaimer / crashpad 的都是子进程。
    """
    comm = (b.get("comm") or "").lower()
    for marker in ("helper", "renderer", "disclaimer", "crashpad",
                   "gpu-process", "utility"):
        if marker in comm:
            return False
    return True


def _parse(agent_type, path, context_limit=None):
    """解析会话文件并返回统一字段"""
    if agent_type == "claude":
        limit = context_limit or sessions.DEFAULT_CONTEXT_LIMIT
        return sessions.parse_claude_session(path, limit)
    if agent_type == "codex":
        return sessions.parse_codex_session(path)
    if agent_type == "opencode":
        return sessions.parse_opencode_session(path)
    return {}


def totals(sess_list):
    """聚合统计"""
    t = {
        "sessions": len(sess_list),
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_create_tokens": 0,
        "cost": 0.0, "mem_mb": 0.0, "cpu_pct": 0.0,
        "by_type": {},
    }
    for s in sess_list:
        # 内存 / CPU 是进程级的，全部累加
        t["mem_mb"] += s.get("mem_mb") or 0.0
        t["cpu_pct"] += s.get("cpu_pct") or 0.0
        bt = t["by_type"].setdefault(s["agent_type"], {"n": 0, "cost": 0.0, "mem": 0.0})
        bt["n"] += 1
        bt["mem"] += s.get("mem_mb") or 0.0
        # token 与成本是会话级的 —— 只算主进程，避免同一会话重复计费
        if not s.get("is_main", True):
            continue
        t["input_tokens"] += s.get("input_tokens") or 0
        t["output_tokens"] += s.get("output_tokens") or 0
        t["cache_read_tokens"] += s.get("cache_read_tokens") or 0
        t["cache_create_tokens"] += s.get("cache_create_tokens") or 0
        t["cost"] += s.get("cost") or 0.0
        bt["cost"] += s.get("cost") or 0.0
    # 会话数按主进程计
    t["sessions"] = sum(1 for s in sess_list if s.get("is_main", True))
    return t


def alerts(sess_list, context_warn=20, mem_warn_mb=2048, idle_warn_s=600):
    """生成告警列表"""
    out = []
    now = time.time()
    for s in sess_list:
        pct = s.get("context_pct")
        if pct is not None and pct <= context_warn:
            out.append({
                "level": "warn", "pid": s["pid"], "agent": s["agent_type"],
                "kind": "CONTEXT_LOW",
                "msg": f"上下文剩余 {pct}%，建议 /compact",
            })
        mem = s.get("mem_mb")
        if mem and mem >= mem_warn_mb:
            out.append({
                "level": "warn", "pid": s["pid"], "agent": s["agent_type"],
                "kind": "MEM_HIGH",
                "msg": f"内存占用 {mem:.0f} MB",
            })
        # 僵尸会话：进程还在但会话文件很久没更新
        sp = s.get("session_path")
        if sp:
            try:
                age = now - os.path.getmtime(sp)
                if age > idle_warn_s:
                    out.append({
                        "level": "info", "pid": s["pid"], "agent": s["agent_type"],
                        "kind": "IDLE",
                        "msg": f"会话已 {int(age / 60)} 分钟无更新",
                    })
            except OSError:
                pass
    return out
