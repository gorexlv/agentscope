# agentscope

本地 AI Agent 会话进程监控与清理工具。

解决一个具体且反复出现的问题：**Claude Code / Codex 等 CLI Agent 会话结束后，
它们派生的子进程（MCP server、dev server、language server、node 进程）不会随之退出，
长期堆积直到拖垮整台机器。**

```
典型症状                        agentscope 的判据
─────────────────────────────────────────────────────────────
load average 飙到 30+          按进程组聚合，一次清掉整组残留
但 CPU 只用了 20%             只清理"根进程已死"的组，活的会话不动
磁盘 IO 持续 6 MB/s            排除 node_modules 的 Spotlight 索引
内存空闲掉到 50%              监听端口的组自动跳过
```

## 快速开始

```bash
git clone https://github.com/gorexlv/agentscope.git
cd agentscope
python3 install/install.py
```

安装后即可使用：

### 进程清理

```bash
agentscope scan                # 列出Agent 会话与残留进程组
agentscope tree <pgid>         # 展开某个进程组看构成
agentscope clean --dry-run     # 预演清理
agentscope clean --yes         # 执行清理
agentscope watch --interval 60 # 守护模式（前台）
```

### AI Agent 会话监控

参考 [ctop](https://github.com/aakashadesara/ctop) 的设计，单终端面板监控
Claude Code / Codex CLI / OpenCode / Devin 会话。

```bash
agentscope panel                # 交互式面板（默认 3s 刷新）
agentscope agents               # 列出所有 Agent 会话
agentscope agents --alerts      # 同时显示告警
agentscope agents --json        # JSON 输出（给脚本用）
agentscope node                 # node/Electron 内存与堆上限诊断
agentscope stats                # 跨会话汇总（token / 成本 / 内存）
agentscope alerts               # 只显示需要关注的会话
agentscope whoami               # 检测当前终端处于哪个 Agent 会话
```

面板输出示例：

```
    PID AGENT        CPU       MEM   CTX    TOKENS     COST MODEL                  BRANCH             CWD
────────────────────────────────────────────────────────────────────────────────────────────────────
* 68220 claude    11%   364M      65%   159       $0.04    claude-opus-5-5        main               …adastralab-ai/money
* 57705 claude    8%    309M      32%   452       $0.08    claude-opus-5-5        orca/PM-2          ~/orca/workspaces/mo-/PM-2
  57815 claude    0%    843M        -       -         -    claude-opus-5-5        orca/PM-2          ~/orca/workspaces/mo-/PM-2
```

`*` 是会话主进程（承载 token 与成本），空格是子进程（Helper / Renderer）——
子进程共享同一份会话文件，计费只算主进程，避免重复统计。

### 持续监控

`--watch` 让任意视图按间隔刷新：

```bash
agentscope agents --watch                      # 持续看会话状态
agentscope agents --watch --sort cpu           # 按 CPU 排序持续监控
agentscope node --watch                        # 持续看 node 内存
agentscope scan --watch                        # 持续看进程组变化
agentscope agents --watch --interval 1         # 1 秒刷新
agentscope agents --watch --no-clear           # 不清屏，逐块追加（适合管道）
agentscope agents --watch --iterations 10      # 刷 10 轮后退出
```

与 `panel` 的区别：

| | `panel` | `--watch` |
|---|---|---|
| 形态 | 交互式 TUI，全屏重绘 | 轻量，同一块内容按间隔刷新 |
| 附带信息 | node 内存段 + 告警 | 只输出该视图的内容 |
| 管道友好 | 否 | `--no-clear` 后可以 |

`--watch` 适合放进另一个终端分屏，或配合 `grep` / `awk` 处理：

```bash
agentscope agents --watch --no-clear | grep -E "claude|codex"
```

### node 堆上限诊断

AI 编程时大量启用 worktree 并行，或同时编译 TS/JS，若 node 堆上限设得太大
（`--max-old-space-size=8192` 之类），内存占用会非常高。

```bash
agentscope node
```

```
node / Electron 内存占用
  14 个进程 · 合计 0.82 GB · 配置了堆上限的 1 个 · 最大 16384 MB

      PID       MEM      HEAP ROLE              WORKTREE
  ──────────────────────────────────────────────────────
    50142 414M      -         dev-server        ~/orca/workspaces/orch
    49815 48M   16384MB   runtime           /

诊断
  ● HEAP_LIMIT_HIGH: 堆上限 16384 MB 偏大（--max-old-space-size），实际占用 48 MB
      → 建议降到 2048 MB（NODE_OPTIONS=--max-old-space-size=2048）
```

诊断规则：
- `HEAP_LIMIT_HIGH` — 堆上限 ≥ 4 GB
- `TOO_MANY_INSTANCES` — 同一 worktree 下 node 实例 ≥ 6 个
- `TOTAL_MEM_HIGH` — node 类进程总占用 ≥ 4 GB

卸载：

```bash
python3 install/install.py --uninstall
```

## 工作原理

### 技术基石

三个内核行为，共同构成整套方案（已在 macOS 26.6 / arm64 实测验证）：

1. 子进程**继承父进程的进程组**（pgid）
2. 父进程退出后，子进程成为孤儿（ppid=1）但**保留原 pgid**
3. 清理前复核完整进程组，再逐个核对 PID + 启动时间发送信号

因此按 pgid 聚合，就能把"某次 Agent 会话残留的全部进程"识别为一个可清理单元。

### 关键判据：如何区分「残留」与「健康」

**最容易踩的坑**：`ppid=1` 并不代表残留。

macOS 上所有桌面应用（Arc、Claude、ChatGPT、Lark、Logseq、Paseo…）都是
`launchd` 拉起后 reparent 到 init 的，根进程 ppid 同样是 1，但它们完全健康。
早期版本按 ppid 判定，差点把整屏桌面应用当垃圾清掉。

真正的判据是**所有根节点同时满足三条条件**：

| 条件 | 说明 |
|---|---|
| 根进程父进程已退出 | ppid 为 1，或父进程不在当前快照中 |
| 根进程自身是 shell / 开发工具 | bash、zsh、fish、node、python… |
| 根进程的 cwd 落在工作区目录内 | 桌面应用的 cwd 是 `/`，据此可精确区分 |

### 安全阀（三重）

1. 组内有 Agent 本体（claude / codex）在跑 → **跳过**，会话正在进行
2. 组内有进程在监听端口 → **跳过**，可能正是你要用的服务
3. 终止前二次校验成员 → 防止"扫描时是残留、清理时刚好被启动"的竞态

此外还保护：当前会话自己的进程树、桌面应用宿主（进程名 + lsof 反查 `.app` 路径 + cwd 位于 `/Applications`）。

## 自动集成

安装器会自动完成：

| 目标 | 机制 |
|---|---|
| **Claude Code** | 注册 `SessionEnd` hook —— 会话结束时自动清理该会话残留 |
| **Codex** | 改写 `config.toml` 的 `notify` 为包装器，包装器内部继续调用原有命令 |
| **macOS** | launchd 守护（`com.agentscope.watchdog`），每 2 分钟巡检一次 |

Claude Code 的 hook 注册是幂等的，且**不破坏已有的 hook**（如 orca / paseo 的集成）。

Codex 侧采用包装器而非直接覆盖，是因为 Codex 的 `notify` 只接受单个命令；
包装器会继续调用原本的 notify 程序，功能不丢。

## 排除 Spotlight 索引

Agent 长期堆积的另一半原因是 Spotlight 在反复扫描 `node_modules`。

```bash
# 扫描并批量写入 .metadata_never_index
python3 install/exclude_index.py --dry-run   # 预演
python3 install/exclude_index.py             # 执行
```

实测效果：mdworker 进程 8 → 2，磁盘 IO 6.4 MB/s → ≈ 0。

脚本默认排除 `node_modules` / `__pycache__` / `.cache` / `coverage` / `.turbo` /
`target` / `.venv`，**不动** `dist` / `build` / `.next`（可能是人工维护的产物）。

## 技术实现说明

### 为什么不用 ps / top

在受限环境（沙箱 / seatbelt）下 `ps`、`top` 会直接被拒绝：

```
PermissionError: [Errno 1] Operation not permitted: 'ps'
```

因此直接读内核进程表：

- `libproc.proc_listallpids()` 枚举全部 PID
- `libproc.proc_pidinfo(pid, PROC_PIDTBSDINFO, ...)` 读取进程信息
  （flavor 3 的字段偏移：`pid@12`、`ppid@16`、`comm@48`、`name@64`）
- `libc.getpgid(pid)` 读取任意进程的进程组

### 性能：逐进程调lsof 是不可行的

逐个进程调用 `lsof` 取 cwd，在500+ 进程时需要 **24 秒** —— hook 的 30 秒
timeout 扛不住。改为一次批量调用：

```bash
lsof -a -d cwd -F pn -p <pid列表>
```

`-F pn` 输出格式是 `p<pid>` 后跟 `n<path>`（**不能用默认表格格式解析**），
整体耗时降到 **1.06 秒，提升约 20 倍**。

### 清理前复核

clean、watch、hook、panel 共用清理入口。先重新扫描并核对成员身份、
父进程、白名单、当前进程树、活跃 Agent 和监听端口；拒绝未知启动时间和其他用户的进程。
每次发送 TERM / KILL 前再次核对 PID、启动时间和 pgid，按子先父后的顺序处理。
内核检查与发信号并非原子操作，仍存在极短竞态窗口；本项目不声称能消除全部 PID 复用风险。


### 判断活跃度不用 CPU 时间

`proc_pidinfo` flavor 2 的 CPU 时间字段解析不可靠（实测数值不合理）。
改用「进程名 + tty 持有」判定：

```bash
lsof -c claude -c node -d 0,1,2
```

交互式 Agent 会话一定持有终端设备，这比 CPU 时间可靠得多。

## 实测数据

在一台 macOS 26.6 / arm64（64 GB）机器上，8 个 workspace 并行跑 Agent 会话：

| 指标 | 处理前 | 处理后 |
|---|---|---|
| load average (1m) | 38.95 | 2.96 |
| 进程总数 | 862 | 690 |
| Swap 占用 | 31.7 GB | 2.6 GB |
| 内存空闲 | 50% | 88% |
| mdworker 进程 | 8 | 2 |
| 磁盘 IO | 6.4 MB/s | ≈ 0 |

## 兼容性

- macOS（依赖 libproc 与 `getpgid`，为 macOS 专属）
- Python 3.9+，仅用标准库，无第三方依赖
- 需要 `lsof`（macOS 自带）

## License

MIT

## 配置、历史与面板操作

配置路径为 `~/.config/agentscope/config.json`，可用 `AGENTSCOPE_CONFIG` 覆盖。
文件缺失使用默认配置；无效 JSON 或字段值会报错并阻止清理。示例：

```json
{
  "workspaces": ["~/Projects", "~/Workbuddy"],
  "protect": {
    "comm": ["WorkBuddy", "Orca", "my-daemon"],
    "cmdline": ["*--keep-running*"],
    "cwd": ["*/important-service/*"]
  },
  "notify": {"enabled": false, "cooldown_minutes": 15},
  "pricing": {
    "my-model": {"input": 1, "output": 2, "cacheWrite": 1.25, "cacheRead": 0.1}
  }
}
```

`protect` 每个字段替换该字段的默认规则；字段之间为 OR，任一成员命中即保护整组。
普通字符串做不区分大小写的子串匹配，包含 `* ? [` 的规则做 glob 匹配。
命令行无法采集且启用了命令行规则时，保守地保护该进程。
默认保护已知桌面应用，当前进程的祖先、同组进程及其后代也受到保护。
工作区按规范化路径边界匹配，`/work` 不会匹配 `/workspace`；默认增加 `~/Workbuddy`。
`AGENTSCOPE_WORKSPACES`（冒号分隔）优先于配置文件，保留兼容性。

通知默认关闭；开启后，watch 和 panel 仅发送 `CONTEXT_LOW`、`MEM_HIGH`，以及 watch
实际清理成功事件。每个监控实例按 PID + 告警类型限频，默认 15 分钟；重启实例后冷却重置。
价格单位为美元 / 百万 token，覆盖项必须包含四项非负单价；非精确型号使用回退价格时，
JSON 提供 `cost_estimated`，会话表和总成本以 `≈` 标识。内置价格是静态参考，未在线核价。

终端面板：`↑↓` / `j k` 选择进程组，Enter 展开进程树，`c` 请求清理，`y` 二次确认
（其他键取消），`s` 切换会话排序，`q` 退出。确认绑定已展示的成员身份，身份变化会拒绝清理。
非 TTY 保持自动刷新模式，Ctrl-C 退出。

```bash
agentscope history --kills --days 7 --json
agentscope history --sessions
```

历史位于 `~/.local/state/agentscope/kills.jsonl` 与 `sessions.jsonl`，可用
`AGENTSCOPE_STATE_DIR` 指定隔离测试目录。新文件权限为 0600。
清理历史包含时间、模式、成员 PID/名称/命令行/cwd、理由与结果；实际清理前先写 attempt，
写入失败则不发送信号。dry-run 只记录预演，不发送信号，即便同时传入 `--yes`。
历史包含本地路径和命令行，请注意其中可能出现的敏感参数。
SessionEnd 即使没有残留也记录会话摘要；传入 transcript_path 时附带 token 和估算成本。
安装器每个目标仅保留最新一份自身生成的时间戳备份，不删除其他命名的文件。

开发验证：`python3 -m unittest discover -s tests`（标准库；信号和通知均 mock）。
