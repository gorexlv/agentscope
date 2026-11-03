"""
会话文件解析：token 用量、上下文窗口、成本、分支。

数据来源（格式已在本机实测确认）：

Claude Code  ~/.claude/projects/<slug>/<sessionId>.jsonl
    每行一个 JSON。含 usage 的 assistant 消息：
        {
          "type": "assistant",
          "message": {
            "model": "claude-opus-5-5",
            "stop_reason": "end_turn",
            "usage": {
              "input_tokens": 2,
              "cache_creation_input_tokens": 276,
              "cache_read_input_tokens": 115297,
              "output_tokens": 107
            }
          },
          "git_branch": "orca/PM-2",
          "cwd": "/path/to/project",
          "session_id": "...",
          "version": "2.1.311"
        }
    上下文占用 = input + cache_creation + cache_read

Codex  ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl
    payload.type == "token_count" 的条目：
        {
          "payload": {
            "type": "token_count",
            "info": {
              "total_token_usage": {
                "input_tokens": 1456909,
                "cached_input_tokens": 1205632,
                "output_tokens": 4442,
                "reasoning_output_tokens": 378
              },
              "last_token_usage": { "input_tokens": 146943, ... },
              "model_context_window": 828400
            }
          }
        }
    注意：cost 用累计值，context 占用用最后一轮的 input_tokens
    （累计值是所有轮次总和，不能代表上下文填充量）

OpenCode  ~/.local/share/opencode/storage/session/...
Devin     终端会话，按 ctop 的做法做尽力而为的识别
"""

import json
import os
import re
from datetime import datetime, timezone

# ── 定价表（USD / 每百万 token）────────────────────────────────
# 取自 ctop 的 MODEL_PRICING，并补充本机实际使用的模型。
MODEL_PRICING = {
    # Claude
    "claude-opus-4-6":      {"input": 5,    "output": 25,   "cacheWrite": 6.25,  "cacheRead": 0.50},
    "claude-opus-5-5":      {"input": 5,    "output": 25,   "cacheWrite": 6.25,  "cacheRead": 0.50},
    "claude-sonnet-4-6":    {"input": 3,    "output": 15,   "cacheWrite": 3.75,  "cacheRead": 0.30},
    "claude-sonnet-5":      {"input": 3,    "output": 15,   "cacheWrite": 3.75,  "cacheRead": 0.30},
    "claude-haiku-4-5":     {"input": 1,    "output": 5,    "cacheWrite": 1.25,  "cacheRead": 0.10},
    # OpenAI
    "gpt-4.1":              {"input": 2,    "output": 8,    "cacheWrite": 0,     "cacheRead": 0.50},
    "gpt-4.1-mini":         {"input": 0.40, "output": 1.60, "cacheWrite": 0,     "cacheRead": 0.10},
    "gpt-4.1-nano":         {"input": 0.10, "output": 0.40, "cacheWrite": 0,     "cacheRead": 0.025},
    "gpt-5":                {"input": 1.25, "output": 10,   "cacheWrite": 0,     "cacheRead": 0.125},
    "gpt-5-codex":          {"input": 1.25, "output": 10,   "cacheWrite": 0,     "cacheRead": 0.125},
    "o3":                   {"input": 2,    "output": 8,    "cacheWrite": 0,     "cacheRead": 0.50},
    "o4-mini":              {"input": 1.10, "output": 4.40, "cacheWrite": 0,     "cacheRead": 0.275},
    "codex-mini-latest":    {"input": 1.50, "output": 6,    "cacheWrite": 0,     "cacheRead": 0.375},
    # Cognition Devin（按 ACU 估算，仅供参考）
    "swe-1-6":              {"input": 3,    "output": 15,   "cacheWrite": 3.75,  "cacheRead": 0.30},
    "swe-1-6-fast":         {"input": 0.80, "output": 4,    "cacheWrite": 1,     "cacheRead": 0.08},
    # 本地模型
    "ollama":               {"input": 0,    "output": 0,    "cacheWrite": 0,     "cacheRead": 0},
}

DEFAULT_CONTEXT_LIMIT = 200_000


def pricing_for(model, agent_type=None):
    """按模型名取价，未命中则按关键词/类型回退"""
    if not model:
        return None
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    m = model.lower()
    if "opus" in m:
        return MODEL_PRICING["claude-opus-5-5"]
    if "haiku" in m:
        return MODEL_PRICING["claude-haiku-4-5"]
    if "sonnet" in m:
        return MODEL_PRICING["claude-sonnet-5"]
    if "gpt-4.1-nano" in m:
        return MODEL_PRICING["gpt-4.1-nano"]
    if "gpt-4.1-mini" in m:
        return MODEL_PRICING["gpt-4.1-mini"]
    if "gpt-4.1" in m or "gpt-5" in m:
        return MODEL_PRICING["gpt-5"]
    if "o3" in m:
        return MODEL_PRICING["o3"]
    if "o4-mini" in m:
        return MODEL_PRICING["o4-mini"]
    if "codex-mini" in m:
        return MODEL_PRICING["codex-mini-latest"]
    if any(k in m for k in ("ollama", "qwen", "llama")):
        return MODEL_PRICING["ollama"]
    if "swe-1" in m and "fast" in m:
        return MODEL_PRICING["swe-1-6-fast"]
    if "swe-1" in m or m.startswith("swe-"):
        return MODEL_PRICING["swe-1-6"]
    if agent_type == "codex":
        return MODEL_PRICING["gpt-5"]
    if agent_type == "opencode":
        return MODEL_PRICING["claude-sonnet-5"]
    if agent_type == "devin":
        return MODEL_PRICING["swe-1-6"]
    return MODEL_PRICING["claude-sonnet-5"]


def calculate_cost(model, input_tokens, output_tokens,
                   cache_create=0, cache_read=0, agent_type=None):
    """按 token 用量算成本（USD）"""
    if input_tokens is None:
        return None
    p = pricing_for(model, agent_type)
    if not p:
        return None
    return (
        (input_tokens or 0) * p["input"] / 1_000_000 +
        (output_tokens or 0) * p["output"] / 1_000_000 +
        (cache_create or 0) * p["cacheWrite"] / 1_000_000 +
        (cache_read or 0) * p["cacheRead"] / 1_000_000
    )


def format_cost(cost):
    if cost is None:
        return "--"
    if cost < 0.01:
        return "<$0.01"
    return f"${cost:.2f}"


def format_tokens(n):
    if n is None:
        return "--"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


# ── Claude Code ────────────────────────────────────────────────

def parse_claude_session(path, context_limit=DEFAULT_CONTEXT_LIMIT):
    """解析 Claude Code 会话文件尾部，取最近一次 assistant 的 usage"""
    from .procstat import read_file_tail

    result = {
        "input_tokens": None, "output_tokens": None,
        "cache_create_tokens": None, "cache_read_tokens": None,
        "context_used": None, "context_limit": context_limit,
        "context_pct": None, "model": None, "stop_reason": None,
        "git_branch": None, "cwd": None, "session_id": None,
        "version": None, "timestamp": None, "title": None,
        "last_turn_ms": None, "compacted": False,
    }
    tail = read_file_tail(path)
    if not tail:
        return result

    for line in reversed(tail):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue

        if d.get("type") in ("custom-title", "ai-title") and not result["title"]:
            result["title"] = d.get("customTitle") or d.get("aiTitle")

        if d.get("subtype") == "turn_duration" and result["last_turn_ms"] is None:
            result["last_turn_ms"] = d.get("durationMs")

        msg = d.get("message") or {}
        usage = msg.get("usage")
        if usage:
            inp = usage.get("input_tokens") or 0
            cc = usage.get("cache_creation_input_tokens") or 0
            cr = usage.get("cache_read_input_tokens") or 0
            out = usage.get("output_tokens") or 0
            used = inp + cc + cr
            result.update({
                "input_tokens": inp,
                "output_tokens": out,
                "cache_create_tokens": cc,
                "cache_read_tokens": cr,
                "context_used": used,
                "context_pct": max(0, min(100, round((context_limit - used) / context_limit * 100))),
                "model": msg.get("model"),
                "stop_reason": msg.get("stop_reason"),
                "git_branch": d.get("gitBranch"),
                "cwd": d.get("cwd"),
                "session_id": d.get("sessionId"),
                "version": d.get("version"),
                "timestamp": d.get("timestamp"),
            })
            if result["context_pct"] is not None and result["context_pct"] < 20:
                result["compacted"] = True
            break

    return result


def claude_session_files(home=None):
    """列出 ~/.claude/projects 下所有会话文件（按 mtime 倒序）"""
    home = home or os.path.expanduser("~")
    root = os.path.join(home, ".claude", "projects")
    out = []
    if not os.path.isdir(root):
        return out
    try:
        for proj in os.listdir(root):
            pdir = os.path.join(root, proj)
            if not os.path.isdir(pdir):
                continue
            try:
                for name in os.listdir(pdir):
                    if not name.endswith(".jsonl"):
                        continue
                    fp = os.path.join(pdir, name)
                    st = os.stat(fp)
                    out.append({
                        "path": fp,
                        "session_id": name[:-6],
                        "project": proj,
                        "mtime": st.st_mtime,
                        "size": st.st_size,
                    })
            except OSError:
                continue
    except OSError:
        return out
    out.sort(key=lambda x: -x["mtime"])
    return out


def cwd_to_slug(cwd):
    """把 cwd 转成 Claude Code 的项目目录名。

    Claude Code 把路径里的 '/' 和 '.' 都换成 '-'：
      /Users/rexlv/orca/workspaces/catch/v0.63.1
        → -Users-rexlv-orca-workspaces-catch-v0-63-1
      /Users/rexlv/Workspace/github.com/adastralab-ai/datum
        → -Users-rexlv-Workspace-github-com-adastralab-ai-datum

    用它可以**不解析文件内容**就定位到会话目录，是性能优化的关键。
    """
    if not cwd:
        return ""
    return cwd.replace("/", "-").replace(".", "-")


def claude_project_dir_to_cwd(slug):
    """把项目目录名还原成路径。

    注意：路径里的 '-' 无法与被替换的 '-' 区分，所以还原只是尽力而为；
    权威来源始终是会话文件里的 cwd 字段。
    """
    if not slug.startswith("-"):
        return ""
    return "/" + slug[1:].replace("-", "/")


# ── Codex ──────────────────────────────────────────────────────

def parse_codex_session(path):
    """解析 Codex 会话文件。

    元数据（cwd / session_id）在文件**头部**的 session_meta 条目里，
    token_count 在**尾部**。所以头尾都要读。
    """
    from .procstat import read_file_tail

    result = {
        "input_tokens": None, "output_tokens": None,
        "cache_create_tokens": None, "cache_read_tokens": None,
        "context_used": None, "context_limit": None, "context_pct": None,
        "model": None, "cwd": None, "git_branch": None,
        "session_id": None, "timestamp": None, "title": None,
    }

    # ── 头部：session_meta ──
    head = read_file_tail(path, max_lines=40, from_head=True)
    for line in head:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        p = d.get("payload") or d
        # session_meta 的 payload 没有 type 字段，但含 session_id / cwd
        if "session_id" in p or ("cwd" in p and "cli_version" in p):
            result["session_id"] = p.get("session_id") or p.get("id")
            result["cwd"] = p.get("cwd")
            result["timestamp"] = p.get("timestamp") or d.get("timestamp")
            break

    # ── 尾部：token_count ──
    tail = read_file_tail(path)
    for line in reversed(tail):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        payload = d.get("payload") or d
        if payload.get("type") == "token_count" and payload.get("info"):
            info = payload["info"]
            cum = info.get("total_token_usage") or {}
            last = info.get("last_token_usage") or {}
            ctx_window = info.get("model_context_window") or DEFAULT_CONTEXT_LIMIT
            inp = cum.get("input_tokens") or 0
            cr = cum.get("cached_input_tokens") or 0
            out = (cum.get("output_tokens") or 0) + (cum.get("reasoning_output_tokens") or 0)
            # 上下文占用用最后一轮的 input_tokens（真正发给模型的量）
            ctx_used = last.get("input_tokens") or cum.get("input_tokens") or 0
            result.update({
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_tokens": cr,
                "cache_create_tokens": cum.get("cache_write_input_tokens") or 0,
                "context_used": ctx_used,
                "context_limit": ctx_window,
                "context_pct": max(0, min(100, round((ctx_window - ctx_used) / ctx_window * 100))),
                "model": payload.get("model") or result.get("model"),
            })
            break

    return result


def codex_session_files(home=None):
    """列出 ~/.codex/sessions 下的会话文件"""
    home = home or os.path.expanduser("~")
    root = os.path.join(home, ".codex", "sessions")
    out = []
    if not os.path.isdir(root):
        return out
    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if not name.endswith(".jsonl"):
                    continue
                fp = os.path.join(dirpath, name)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                sid = None
                m = re.search(r"rollout-.*?-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                              r"[0-9a-f]{4}-[0-9a-f]{12})\.jsonl", name)
                if m:
                    sid = m.group(1)
                out.append({"path": fp, "session_id": sid, "mtime": st.st_mtime,
                            "size": st.st_size})
    except OSError:
        return out
    out.sort(key=lambda x: -x["mtime"])
    return out


# ── OpenCode ───────────────────────────────────────────────────

def opencode_session_files(home=None):
    """OpenCode 会话（存储布局随版本变化，尽力枚举）"""
    home = home or os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".local", "share", "opencode", "storage", "session"),
        os.path.join(home, ".local", "share", "opencode", "storage"),
        os.path.join(home, ".config", "opencode", "storage", "session"),
    ]
    out = []
    for root in candidates:
        if not os.path.isdir(root):
            continue
        try:
            for dirpath, _d, files in os.walk(root):
                for name in files:
                    if not name.endswith(".json"):
                        continue
                    fp = os.path.join(dirpath, name)
                    try:
                        st = os.stat(fp)
                    except OSError:
                        continue
                    out.append({"path": fp, "session_id": name[:-5],
                                "mtime": st.st_mtime, "size": st.st_size})
        except OSError:
            continue
        if out:
            break
    out.sort(key=lambda x: -x["mtime"])
    return out


def parse_opencode_session(path):
    """OpenCode 会话是单个 JSON 文件"""
    result = {"input_tokens": None, "output_tokens": None,
              "cache_create_tokens": None, "cache_read_tokens": None,
              "context_used": None, "context_limit": None, "context_pct": None,
              "model": None, "cwd": None, "git_branch": None,
              "session_id": None, "timestamp": None, "title": None}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            d = json.load(f)
    except Exception:
        return result

    if isinstance(d, dict):
        result["title"] = d.get("title")
        result["cwd"] = d.get("directory") or d.get("cwd")
        result["session_id"] = d.get("id") or d.get("sessionID")
        result["git_branch"] = d.get("gitBranch")
        result["model"] = d.get("modelID") or d.get("model")
        ts = d.get("time") or {}
        if isinstance(ts, dict):
            t = ts.get("created") or ts.get("updated")
            if isinstance(t, (int, float)) and t > 0:
                if t > 1e12:
                    t /= 1000
                try:
                    result["timestamp"] = datetime.fromtimestamp(
                        t, tz=timezone.utc).isoformat()
                except Exception:
                    pass
        tokens = d.get("tokens") or {}
        if isinstance(tokens, dict):
            inp = tokens.get("input")
            out = tokens.get("output")
            cache = tokens.get("cache") or {}
            reasoning = tokens.get("reasoning") or {}
            if inp is not None:
                result["input_tokens"] = inp
                result["output_tokens"] = (out or 0) + (reasoning or 0)
                result["cache_read_tokens"] = cache.get("read") or 0
                result["cache_create_tokens"] = cache.get("write") or 0
                used = inp + result["cache_read_tokens"] + result["cache_create_tokens"]
                limit = tokens.get("limit") or DEFAULT_CONTEXT_LIMIT
                result["context_used"] = used
                result["context_limit"] = limit
                result["context_pct"] = max(0, min(100, round((limit - used) / limit * 100)))
    return result
