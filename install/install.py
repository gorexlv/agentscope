#!/usr/bin/env python3
"""
agentscope 安装器

一键安装到 ~/.local/bin，并注册到 Claude Code / Codex 的生命周期钩子。

用法：
  python3 install/install.py              # 完整安装
  python3 install/install.py --uninstall  # 卸载（含移除所有钩子注册）
  python3 install/install.py --dry-run    # 预演，不做实际修改
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
BIN_DIR = os.path.join(HOME, ".local", "bin")
LIB_DIR = os.path.join(HOME, ".local", "lib")
STATE_DIR = os.path.join(HOME, ".local", "state", "agentscope")
LAUNCH_DIR = os.path.join(HOME, "Library", "LaunchAgents")

SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN_SRC = os.path.join(SRC_DIR, "bin")

CLAUDE_SETTINGS = os.path.join(HOME, ".claude", "settings.json")
CODEX_CONFIG = os.path.join(HOME, ".codex", "config.toml")
PLIST_PATH = os.path.join(LAUNCH_DIR, "com.agentscope.watchdog.plist")

MARKER = "agentscope"          # 用于识别本工具注入的配置
HOOK_NAME = "agentscope-hook"


def log(msg):
    print(msg)


def run(cmd, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60, **kw)
    except Exception as e:
        return None


def backup(path):
    if not os.path.exists(path):
        return None
    b = f"{path}.bak.agentscope.{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, b)
    return b


# ── 1. 安装可执行文件 ──────────────────────────────────────────
def install_bins(dry=False):
    os.makedirs(BIN_DIR, exist_ok=True)
    installed = []
    for name in ("agentscope", "agentscope-hook", "codex-notify-wrapper.sh"):
        src = os.path.join(BIN_SRC, name)
        dst = os.path.join(BIN_DIR, name)
        if not os.path.exists(src):
            log(f"  ✗ 缺少源文件: {src}")
            continue
        if dry:
            log(f"  [dry-run] 将安装 {dst}")
            continue
        if os.path.exists(dst):
            b = backup(dst)
            if b:
                log(f"  · 已备份旧版 -> {os.path.basename(b)}")
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755)
        installed.append(dst)
        log(f"  ✓ {dst}")

    # 安装 Python 包（Agent 监控模块）
    # 装到 ~/.local/lib/agentscope_lib/ 而不是 ~/.local/bin/ —— 否则会与
    # 可执行文件 agentscope 同名冲突（一个是目录一个是文件）
    pkg_src = os.path.join(SRC_DIR, "agentscope_lib")
    pkg_dst = os.path.join(LIB_DIR, "agentscope_lib")
    if os.path.isdir(pkg_src):
        if dry:
            log(f"  [dry-run] 将安装 Python 包 -> {pkg_dst}")
        else:
            if os.path.isdir(pkg_dst):
                shutil.rmtree(pkg_dst)
            shutil.copytree(pkg_src, pkg_dst)
            # 清理可能存在的 __pycache__
            for root, dirs, files in os.walk(pkg_dst):
                for d in list(dirs):
                    if d == "__pycache__":
                        shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                        dirs.remove(d)
            installed.append(pkg_dst)
            log(f"  ✓ {pkg_dst}/ (Agent 监控模块)")
    return installed


# ── 2. 注册 Claude Code SessionEnd hook ────────────────────────
def install_claude_hook(dry=False):
    if not os.path.exists(CLAUDE_SETTINGS):
        log(f"  ✗ 未找到 {CLAUDE_SETTINGS}，跳过 Claude Code 集成")
        return False
    if dry:
        log("  [dry-run] 将向 ~/.claude/settings.json 注册 SessionEnd hook")
        return True

    b = backup(CLAUDE_SETTINGS)
    if b:
        log(f"  · 已备份 -> {os.path.basename(b)}")

    with open(CLAUDE_SETTINGS, encoding="utf-8") as f:
        d = json.load(f)

    hooks = d.setdefault("hooks", {})
    se = hooks.setdefault("SessionEnd", [])

    # 幂等：先移除旧的，再追加
    cleaned = []
    for grp in se:
        keep = [h for h in grp.get("hooks", []) if HOOK_NAME not in h.get("command", "")]
        if keep:
            cleaned.append({**grp, "hooks": keep})
        elif grp.get("hooks"):
            log("  · 移除一条失效的旧 hook 条目")
    cleaned.append({
        "hooks": [{
            "type": "command",
            "command": f'"{os.path.join(BIN_DIR, HOOK_NAME)}" session-end',
            "timeout": 30,
            "statusMessage": "清理会话残留进程",
        }]
    })
    hooks["SessionEnd"] = cleaned

    with open(CLAUDE_SETTINGS, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write("\n")

    n = sum(1 for g in cleaned for h in g.get("hooks", []) if HOOK_NAME in h.get("command", ""))
    log(f"  ✓ SessionEnd hook 已注册（{n} 条，原有 hook 完整保留）")
    return True


# ── 3. 配置 Codex notify ───────────────────────────────────────
def install_codex_notify(dry=False):
    if not os.path.exists(CODEX_CONFIG):
        log(f"  ✗ 未找到 {CODEX_CONFIG}，跳过 Codex 集成")
        return False
    if dry:
        log("  [dry-run] 将改写 ~/.codex/config.toml 的 notify 配置")
        return True

    b = backup(CODEX_CONFIG)
    if b:
        log(f"  · 已备份 -> {os.path.basename(b)}")

    with open(CODEX_CONFIG, encoding="utf-8") as f:
        content = f.read()

    wrapper = os.path.join(BIN_DIR, "codex-notify-wrapper.sh")

    # 记录原始 notify 程序路径，供包装器调用（以及卸载时还原）
    m = re.search(r'^notify\s*=\s*\[.*?\]\s*$', content, re.MULTILINE)
    if m and "codex-notify-wrapper" not in m.group(0):
        orig = re.search(r'"(/[^"]+)"', m.group(0))
        if orig:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(os.path.join(STATE_DIR, "codex_original_notify"), "w",
                      encoding="utf-8") as f:
                f.write(orig.group(1) + "\n")
            log(f"  · 已记录原始 notify -> {orig.group(1)}")

    new_line = f'notify = [ "{wrapper}", "agent-turn-complete" ]'

    pattern = re.compile(r'^notify\s*=\s*\[.*\]\s*$', re.MULTILINE)
    if pattern.search(content):
        content = pattern.sub(new_line, content, count=1)
        log("  ✓ 已替换 notify 配置（原命令保留在包装器内）")
    else:
        content = new_line + "\n" + content
        log("  ✓ 已新增 notify 配置")

    with open(CODEX_CONFIG, "w", encoding="utf-8") as f:
        f.write(content)
    return True


# ── 4. 安装 launchd 守护 ───────────────────────────────────────
PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.agentscope.watchdog</string>

    <key>ProgramArguments</key>
    <array>
        <string>{agentscope_bin}</string>
        <string>watch</string>
        <string>--interval</string>
        <string>{interval}</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:{bin_dir}</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>{state_dir}/watchdog.log</string>

    <key>StandardErrorPath</key>
    <string>{state_dir}/watchdog.err.log</string>

    <key>ProcessType</key>
    <string>Background</string>

    <key>LowPriorityIO</key>
    <true/>

    <key>Nice</key>
    <integer>5</integer>
</dict>
</plist>
"""


def install_watchdog(interval=120, dry=False):
    os.makedirs(LAUNCH_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)

    if dry:
        log("  [dry-run] 将写入 launchd plist")
        return True

    if os.path.exists(PLIST_PATH):
        b = backup(PLIST_PATH)
        if b:
            log(f"  · 已备份 -> {os.path.basename(b)}")

    content = PLIST_TEMPLATE.format(
        agentscope_bin=os.path.join(BIN_DIR, "agentscope"),
        interval=interval,
        bin_dir=BIN_DIR,
        state_dir=STATE_DIR,
    )
    with open(PLIST_PATH, "w", encoding="utf-8") as f:
        f.write(content)

    r = run(["plutil", "-lint", PLIST_PATH])
    if r and r.returncode == 0:
        log(f"  ✓ plist 写入并校验通过 -> {PLIST_PATH}")
    else:
        log(f"  ✗ plist 校验失败: {r.stderr.strip() if r else 'unknown'}")

    # enable 让它在登录时自动加载
    r = run(["launchctl", "enable", f"gui/{os.getuid()}/com.agentscope.watchdog"])
    if r and r.returncode == 0:
        log("  ✓ launchctl enable 成功（下次登录自动加载）")
    else:
        log(f"  ! launchctl enable 失败（不影响手动运行）")

    # 尝试立即加载（部分受限环境会失败，属正常）
    run(["launchctl", "bootout", f"gui/{os.getuid()}/com.agentscope.watchdog"])
    time.sleep(0.4)
    r = run(["launchctl", "bootstrap", f"gui/{os.getuid()}", PLIST_PATH])
    if r and r.returncode == 0:
        log("  ✓ 守护进程已启动")
    else:
        log("  ! 当前环境无法立即加载（受限环境常见），下次登录会自动生效")
    return True


# ── 卸载 ───────────────────────────────────────────────────────
def uninstall(dry=False):
    log("\n卸载 agentscope\n")

    # 移除可执行文件与 Python 包
    for name in ("agentscope", "agentscope-hook", "codex-notify-wrapper.sh"):
        p = os.path.join(BIN_DIR, name)
        if os.path.exists(p):
            if dry:
                log(f"  [dry-run] 将删除 {p}")
            else:
                os.unlink(p)
                log(f"  ✓ 已删除 {p}")

    pkg = os.path.join(LIB_DIR, "agentscope_lib")
    if os.path.isdir(pkg):
        if dry:
            log(f"  [dry-run] 将删除 {pkg}/")
        else:
            shutil.rmtree(pkg)
            log(f"  ✓ 已删除 {pkg}/")

    # 移除 Claude hook
    if os.path.exists(CLAUDE_SETTINGS):
        with open(CLAUDE_SETTINGS, encoding="utf-8") as f:
            d = json.load(f)
        se = d.get("hooks", {}).get("SessionEnd", [])
        cleaned = []
        removed = 0
        for grp in se:
            keep = [h for h in grp.get("hooks", []) if HOOK_NAME not in h.get("command", "")]
            removed += len(grp.get("hooks", [])) - len(keep)
            if keep:
                cleaned.append({**grp, "hooks": keep})
        if removed:
            d["hooks"]["SessionEnd"] = cleaned
            if not dry:
                b = backup(CLAUDE_SETTINGS)
                with open(CLAUDE_SETTINGS, "w", encoding="utf-8") as f:
                    json.dump(d, f, ensure_ascii=False, indent=2)
                    f.write("\n")
            log(f"  ✓ 移除 {removed} 个 Claude Code hook")

    # 还原 Codex notify
    if os.path.exists(CODEX_CONFIG):
        with open(CODEX_CONFIG, encoding="utf-8") as f:
            content = f.read()
        if "codex-notify-wrapper" in content:
            restored_line = None
            # 优先用安装时记录的原始 notify 路径
            record = os.path.join(STATE_DIR, "codex_original_notify")
            if os.path.exists(record):
                try:
                    with open(record, encoding="utf-8") as rf:
                        orig = rf.read().strip()
                    if orig:
                        restored_line = f'notify = [ "{orig}", "turn-ended" ]'
                except Exception:
                    pass
            # 回退：从备份里找
            if not restored_line:
                backups = sorted(
                    (os.path.join(os.path.dirname(CODEX_CONFIG), f)
                     for f in os.listdir(os.path.dirname(CODEX_CONFIG))
                     if f.startswith("config.toml.bak.agentscope.")),
                    reverse=True)
                for bk in backups:
                    try:
                        with open(bk, encoding="utf-8") as bf:
                            bcontent = bf.read()
                        m = re.search(r'^notify\s*=\s*\[.*\]\s*$', bcontent, re.MULTILINE)
                        if m and "codex-notify-wrapper" not in m.group(0):
                            restored_line = m.group(0)
                            break
                    except Exception:
                        continue

            if restored_line:
                pattern = re.compile(r'^notify\s*=\s*\[.*\]\s*$', re.MULTILINE)
                content = pattern.sub(restored_line, content, count=1)
                if not dry:
                    with open(CODEX_CONFIG, "w", encoding="utf-8") as f:
                        f.write(content)
                log("  ✓ 已还原 Codex notify 配置")
            else:
                log("  ! 未找到可还原的原始 notify，请手动检查 config.toml")

    record = os.path.join(STATE_DIR, "codex_original_notify")
    if os.path.exists(record) and not dry:
        os.unlink(record)

    # 停止并删除 launchd
    run(["launchctl", "bootout", f"gui/{os.getuid()}/com.agentscope.watchdog"])
    if os.path.exists(PLIST_PATH):
        if dry:
            log(f"  [dry-run] 将删除 {PLIST_PATH}")
        else:
            os.unlink(PLIST_PATH)
            log(f"  ✓ 已删除 {PLIST_PATH}")

    log("\n卸载完成。")


def main():
    ap = argparse.ArgumentParser(description="agentscope 安装器")
    ap.add_argument("--uninstall", action="store_true", help="卸载")
    ap.add_argument("--dry-run", action="store_true", help="只预演不修改")
    ap.add_argument("--interval", type=int, default=120, help="守护进程扫描间隔（秒）")
    args = ap.parse_args()

    if args.uninstall:
        uninstall(args.dry_run)
        return 0

    log(f"\n安装 agentscope{'（预演模式）' if args.dry_run else ''}\n")

    log("[1/4] 安装可执行文件")
    install_bins(args.dry_run)

    log("\n[2/4] 注册 Claude Code SessionEnd hook")
    install_claude_hook(args.dry_run)

    log("\n[3/4] 配置 Codex notify")
    install_codex_notify(args.dry_run)

    log("\n[4/4] 安装 launchd 守护进程")
    install_watchdog(args.interval, args.dry_run)

    if not args.dry_run:
        log("\n" + "=" * 60)
        log("安装完成。常用命令：")
        log("  agentscope scan                列出 Agent 会话与残留进程组")
        log("  agentscope tree <pgid>         查看进程组构成")
        log("  agentscope clean --dry-run     预演清理")
        log("  agentscope clean --yes         执行清理")
        log("  agentscope watch --interval 60 守护模式（前台）")
        log("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
