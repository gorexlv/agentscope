#!/bin/bash
# Codex notify 包装器
#
# Codex 的 config.toml 里 notify 只能配一个命令。
# 本包装器做两件事：
#   1. 把原始 notify 程序继续调用，不破坏现有功能
#   2. 转发给 agentscope-hook，清理该会话遗留的子进程
#
# 原始 notify 程序的路径从 Codex 配置文件里自动读取；
# 读不到时回退到常见的默认安装位置。
#
# Codex 传给 notify 的参数：
#   第 1 个 = 事件类型（如 agent-turn-complete）
#   第 2 个 = JSON 字符串（含 type / thread-id / cwd / last-assistant-message 等）

set -u

CODEX_CONFIG="${CODEX_CONFIG:-$HOME/.codex/config.toml}"
HOOK="${AGENTSCOPE_HOOK:-$HOME/.local/bin/agentscope-hook}"

# 从 config.toml 里解析出原始 notify 命令
# 注意：必须排除包装器自身，否则会形成自我调用循环
ORIG_NOTIFY=""
if [ -f "$CODEX_CONFIG" ]; then
  line=$(grep -E '^[[:space:]]*notify[[:space:]]*=' "$CODEX_CONFIG" | head -1)
  if [ -n "$line" ]; then
    # 提取第一个带引号的绝对路径
    candidate=$(printf '%s' "$line" | sed -n 's/.*"\(\/[^"]*\)".*/\1/p')
    case "$candidate" in
      *codex-notify-wrapper*) : ;;   # 指向自己，跳过
      *) ORIG_NOTIFY="$candidate" ;;
    esac
  fi
fi

# 优先使用安装时记录的原始 notify 路径
RECORD="${AGENTSCOPE_STATE:-$HOME/.local/state/agentscope}/codex_original_notify"
if [ -f "$RECORD" ]; then
  recorded=$(head -1 "$RECORD")
  if [ -n "$recorded" ] && [ -x "$recorded" ]; then
    ORIG_NOTIFY="$recorded"
  fi
fi

# 回退：常见的默认安装位置
if [ -z "$ORIG_NOTIFY" ] || [ ! -x "$ORIG_NOTIFY" ]; then
  for cand in \
    "$HOME/.codex/computer-use/Codex Computer Use.app/Contents/SharedSupport/SkyComputerUseClient.app/Contents/MacOS/SkyComputerUseClient" \
    "/Applications/Codex.app/Contents/SharedSupport/SkyComputerUseClient.app/Contents/MacOS/SkyComputerUseClient"
  do
    if [ -x "$cand" ]; then
      ORIG_NOTIFY="$cand"
      break
    fi
  done
fi

EVENT="${1:-}"
PAYLOAD="${2:-}"

# 1) 保留原有 notify 行为
if [ -n "$ORIG_NOTIFY" ] && [ -x "$ORIG_NOTIFY" ]; then
  "$ORIG_NOTIFY" "$EVENT" "$PAYLOAD" >/dev/null 2>&1 &
fi

# 2) 交给 agentscope 清理会话残留
#    仅在 turn 完成时触发，避免每个事件都扫一遍进程表
case "$EVENT" in
  agent-turn-complete|turn-ended|agent-turn-complete-with-approval)
    if [ -x "$HOOK" ]; then
      printf '%s' "$PAYLOAD" | "$HOOK" session-end >/dev/null 2>&1 &
    fi
    ;;
  *)
    : # 其它事件不处理
    ;;
esac

exit 0
