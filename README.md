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
| 根进程父进程已退出 | ppid 为 1，或直接探测确认不存在；权限不足视为存活 |
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
文件缺失使用默认配置；无效 JSON、字段值或读取错误会在 stderr 警告，只读显示回退默认值且关闭通知，清理记录 skipped 并跳过，守护进程继续下一轮。配置按文件版本缓存，变更后重新解析。示例：

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
默认 comm 规则匹配桌面应用名前缀，cmdline 仅匹配 app bundle 路径，不会因工作区路径含 orca 而保护开发脚本。当前进程的祖先、同组进程及其后代也受到保护。
工作区按规范化路径边界匹配，`/work` 不会匹配 `/workspace`；默认增加 `~/Workbuddy`。
`AGENTSCOPE_WORKSPACES`（冒号分隔）优先于配置文件，保留兼容性。

通知默认关闭；开启后，watch 和 panel 仅发送 `CONTEXT_LOW`、`MEM_HIGH`，以及 watch
实际清理成功事件。每个监控实例按 PID + 告警类型限频，默认 15 分钟；重启实例后冷却重置。
价格单位为美元 / 百万 token，覆盖项必须包含四项非负单价；非精确型号使用回退价格时，
JSON 提供 `cost_estimated`，会话表和总成本以 `≈` 标识。内置价格是静态参考，未在线核价。

终端面板：`↑↓` / `j k` 选择进程组，Enter 展开进程树，`c` 请求清理，`y` 二次确认
（其他键取消），`s` 切换会话排序，`q` 退出。确认绑定根进程与已展示成员身份；新增子进程不继承清理授权。
采集和清理在同一后台线程执行，输入保持响应；进程组每 20 秒采集一次，清理前重新验证。
SIGTERM/SIGHUP 与异常退出都会恢复终端。
非 TTY 保持自动刷新模式，Ctrl-C 退出。

```bash
agentscope history --kills --days 7 --json
agentscope history --sessions
```

历史位于 `~/.local/state/agentscope/kills.jsonl` 与 `sessions.jsonl`，可用
`AGENTSCOPE_STATE_DIR` 指定隔离测试目录。新文件权限为 0600。
清理历史包含时间、模式、成员 PID/名称/命令行/cwd、理由与结果；实际清理前先写 attempt，
审计失败只写 stderr 警告，不阻止清理，也不改变清理结果。dry-run 复用当前快照记录预演，不发送信号，即便同时传入 `--yes`；普通 clean 未确认时只打印列表。
历史包含本地路径和命令行，请注意其中可能出现的敏感参数。
SessionEnd 即使没有残留也记录会话摘要；传入 transcript_path 时附带 token 和估算成本。
安装器每个目标仅保留最新一份自身生成的时间戳备份，不删除其他命名的文件。

开发验证：`python3 -m unittest discover -s tests`（标准库）。专用集成用例只对自身派生、核对过启动身份的进程发送真实信号；其余信号、通知和面板清理均 mock。

端口采集覆盖所有 TCP LISTEN 地址，失败或超时标为未知并禁止清理。
清理结果区分 `gone`（自行退出）、`cleaned`、`remaining`；僵尸进程视为已退出，KILL 后最多轮询 1.5 秒。
clean 实际清理未全部成功时返回非零。

hook 在进程内分析一次，总预算 25 秒（stdin 最多 1 秒）；后续仅复核目标组元数据和端口。
到期跳过剩余组，SIGTERM/SIGHUP 或解析异常也尽量写入会话摘要。
精确祖先匹配没有可清理目标时，按会话 cwd 或其子目录回退，不匹配 cwd 的父目录。
同目录下其他已确认的孤儿也可能被选中，因此它不是基于 session_id 的严格归属证明。
launchd 拉起的长期 shell/python 工作任务仍可能符合残留判据，请为这类任务配置白名单。
备份只保留最新一份，重复安装会替换首次安装前备份；Codex 卸载还原依赖 codex_original_notify 记录。

## 会话与机器资源视图

```bash
agentscope mcp --json                  # MCP 名称、重复实例、RSS 合计及会话归属
agentscope ports --json                # 所有 TCP LISTEN 地址、会话 PID 与 worktree
agentscope disk --json                 # 生成目录大小与已删除 worktree 的残留线索
agentscope doctor --json               # 只读本地凭据状态、历史额度及 hook/notify 检查
agentscope timeline --since 2h --json   # 最近两小时 watch 采样（也接受 ISO 时间）
agentscope run --heavy --max-load 4 -- npm run build
agentscope run --heavy --dry-run -- npm run build
```

MCP 通过命令行中的已知 server/package 名模式识别，按名字聚合实例数和 RSS。
会话归属优先祖先链，退化 cwd 匹配时明确标记；同目录多个会话标为 ambiguous，不能据此自动清理重复实例。
agents 与 panel 展示归属到会话的 MCP，mcp 命令也展示没有找到会话的实例。
RSS 与 footprint 的物理内存统计口径不同；采集不全时标为 partial。

会话状态包括 WAITING_INPUT、WAITING_PERMISSION、WORKING、IDLE、SUSPECTED_STALL、SUSPECTED_LOOP、UNKNOWN。
状态由最后有效事件、日志更新时间和 CPU 判断：无写入超过 120 秒且低 CPU 标空闲，超过 600 秒的未完成操作且低 CPU 标疑似卡住，高 CPU 标疑似循环。
这些都是提示，不作为清理判据。多个主会话共用规范化后的 cwd 时，显示 CWD_CONFLICT，提示可能同时修改文件。
当前祖先链能识别为 Orca 时保护整个进程组；祖先链不可读或循环时同样保护，不读取和输出进程环境变量。

### 显式维护与排队

端口默认始终受到保护。仅当监听者 cwd 位于配置工作区且确认路径已不存在时标 abandoned，
`clean --abandoned-servers --dry-run` 可检查建议；实际操作仍需 `--yes`，
并且必须通过父进程退出、Orca/白名单/当前树保护、配置和端口状态、成员身份等二次校验。
该开关不用于 watch、hook 或 panel，也不会放行有活父进程的服务。

磁盘视图只统计配置工作区内有限深度的 node_modules、.venv、dist、build、.next、target。
单次盘点预算 8 秒，超时输出 complete=false 或 size_complete=false，不把部分大小当完整大小。
历史会话里已删除的目录、Git worktree 的失效 gitdir 元数据也会列出；后者只建议检查 git worktree prune --dry-run。
`disk --clean /absolute/worktree/node_modules --yes` 才会删除明确选中的生成目录：
禁止工作区根或非白名单目录，并重新检查相关进程；有项目标记且被 Git 忽略的产物符号链接只 unlink 链接本身；仍有进程使用工作区或 cwd 无法完整采集时跳过。
磁盘清理不会杀进程，审计记录在 kills.jsonl 的 disk 模式。检查与文件删除之间仍有非原子竞态，请先停止工作区任务。

重任务用默认 `/tmp/agentscope-heavy.lock` 的 flock 在同机协作实例之间排队，
1 分钟系统负载低于阈值后才执行（默认阈值为逻辑 CPU 数）。可用 `--wait-timeout 秒` 限制等待，超时返回 75；命令退出码原样返回。
只有使用此入口的任务参与排队。`AGENTSCOPE_HEAVY_LOCK` 可用于隔离测试，不要在正常运行中改成每工作区不同的路径。

### 本地健康与时间序列

doctor 不联网，不调用模型或任何 CLI 登录命令。它只报告 Claude/Codex 本地凭据文件是否包含已识别结构、
可得的过期提示、最近五份 Codex 日志中的 rate_limits，以及 SessionEnd/notify 配置和包装脚本标记。
绝不输出 token；present-unverified 不代表服务端登录有效，额度是历史记录而非实时余额。

watch 每轮写 metrics.jsonl：负载、系统 free/active 内存、各 Agent PID 的 RSS 与原始累计 CPU 计数、组数。
首次样本不计算 CPU 百分比；原始计数用于趋势比较，不能与进程快照的百分比直接混用。
采样复用当前快照，不调用重量级 agents.collect；通知关闭时也会采样。
日志达到 1 MiB 后轮转，保留三份归档；timeline 同时读取当前文件和归档，忽略损坏行。
指标采样或落盘失败只警告，不中断清理。测试覆盖 13–21 各项，真实信号仍只限测试自身创建的进程。

磁盘删除进一步要求：目标位于有 `.git` 标记的 worktree 中，同级具有匹配的项目文件。
node_modules/dist/.next/build 要求 package.json，.venv 要求 pyproject.toml、requirements*.txt 或 setup.py，
target 要求 Cargo.toml 或 pom.xml，build 也接受 build.gradle/build.gradle.kts。
不满足条件的同名目录仅显示占用，deletable=false，不提供删除建议，也不能通过 --yes 绕过。

重任务默认最多等待 300 秒，每 15 秒打印排队状态；可显式用 --wait-timeout 0 选择不限时。
持锁子命令会继承锁 fd 和 AGENTSCOPE_HEAVY_HELD=1，嵌套调用直接执行，避免自锁。
包装进程被终止后，子命令持有的 fd 会让锁保持到子命令退出；若子命令主动关闭所有非标准 fd，这种保证不再成立。
排队是轮询竞争，不保证 FIFO；子命令退出或异常时 fd 关闭并释放锁，不删除锁文件。
Claude Code 也可能把凭据存放在 macOS Keychain，doctor 的 missing 只代表未找到本地凭据文件，不代表未登录。

### Worktree 与陈旧仓库回收

```sh
agentscope worktrees --json
agentscope worktrees --online              # 显式通过 gh 查询 PR
agentscope worktrees --fetch               # 显式刷新远端引用
agentscope worktrees clean                 # 默认仅预演
agentscope worktrees clean --include worktrees,prune,branches,slim --yes
```

默认从配置工作区（包括 ~/orca/workspaces、~/.codex/worktrees、~/multica_workspaces*）
发现 Git 仓库，再以 `git worktree list --porcelain -z` 盘点所有登记路径，包括根之外的 linked worktree。
输出 items、branches、artifacts 和 summary；category 为 worktrees（可删）、slim（只瘦身）、manual（需人工）、prune（失效登记）。
本地分支另列 branches。每项 reasons 给出拒绝原因，未知进程 cwd 或 Git 查询失败均拒绝删除。
默认总预算 90 秒，可用 `--budget N` 调整；按仓库四线程并发，先采集证据，仅留最多 2 秒余量给大小；超时 `complete=false`，大小不完整会标 `size_complete=false`，不要把部分盘点理解成全机总量。
路径和根目录 mtime 对应的大小缓存保留最多 24 小时，`size_cached=true` 表示估计值，嵌套文件变化可能尚未反映。

配置文件可增加：

```json
{"worktrees": {"idle_days": 30, "stale_days": 60, "protect": ["~/Projects/keep"]}}
```

`--days N` 临时覆盖 idle_days；protect 为路径及其子树，包含保护路径的祖先 worktree 也不会删除。
现有 protect.cwd 规则同样生效。最后活动取源码目录/文件 mtime 与 worktree HEAD reflog mtime 的较新值，
不扫描依赖及 Git 对象内部；这是保守的本地活动估计，并非完整会话历史。

删除 worktree 必须同时满足：非主目录、存在、干净（含未跟踪文件及下述 ignored 检查）、无本地未推送提交、
已合入 origin 默认分支或对应 HEAD 的 PR 已合并/关闭或达到空闲阈值、无进程使用、未锁定、不受保护。
默认分支来自本地 origin/HEAD；不存在时合入状态为未知，不猜测 main/master，也不隐式联网。
未推送按 HEAD 不属于任何远端引用的提交数计算；远端引用可能过期，可自行显式 fetch。
Squash 合并只能用 --online 的 PR 证据补充。Orca 路径调用 `orca worktree rm`，没有 Orca CLI 则需人工。
只使用普通 `git worktree remove`、`git worktree prune` 和 `git branch -d`，不用 force、-D 或删除远端分支。
prune 是整仓操作，因此会重新检查该仓库所有缺失登记，任一个锁定或受保护都拒绝该仓 prune。

陈旧主仓库可以只瘦身，仍要求无改动、无未推送提交和无进程使用；仅选择项目标记匹配、
没有 Git 跟踪内容的生成目录，实际删除复用 disk.clean 的进程及目录身份检查。
每个候选在执行前重新查询与检查，结果写 worktrees.jsonl；瘦身另有 kills.jsonl 的 disk 审计。
进程启动、文件修改与删除之间仍有非原子窗口，清理前应停止相关任务；任何复核拒绝都会保留目标。
watch 每 7 天盘点并写 worktree-summary.jsonl，通知开启时提醒估计回收量，绝不会自动删除目录或分支。

被 Git 忽略的文件也需要保护。仅放行 node_modules、.venv、dist、build、.next、target
及 __pycache__、.pytest_cache、.ruff_cache、.mypy_cache 目录里的忽略内容，仍须匹配项目标记文件。
Python 缓存可匹配同一 worktree 内祖先目录的 pyproject.toml、requirements*.txt 或 setup.py。
不放行任意缓存目录、CACHEDIR.TAG、独立的 *.pyc 或 .DS_Store 文件。
其余 ignored 内容（例如 .env、本地文档）一律归 manual，reasons 包含 ignored_files，
ignored_paths 展示最多五条路径，ignored_count 为 Git 返回的拒绝条目数（忽略目录可能合并成一条）。
执行前重复检查，不提供强删开关。包含子模块的 worktree 也归 manual。
Detached HEAD 只有确认合入默认分支才能删除；空闲、远端跟踪引用或 PR 状态都不能替代合入证据。
瘦身不会穿过嵌套仓库/worktree，生成目录内部包含仓库、被跟踪或有 Git 状态改动时同样拒绝。
summary.counts.unknown 单列证据不全的数量，是 manual 的子集；不能与其他档相加。
每周汇总包含 unknown 与 complete，通知在不完整时注明“部分盘点”，回收量不能当作精确趋势。
watch 内的周盘点在后台守护线程中执行，采用 90 秒预算，主循环不等待；同一状态目录最多一个任务。
使用墙上时钟记录尝试时间，即使审计或采集失败，也不会在七天内逐轮重试；退出 watch 不等待后台线程。
仓库检查时间持久化到 worktree-checks.json，下次先检查最久未检查的仓库，降低固定顺序造成的饥饿。
本地分支用 for-each-ref --merged 批量判断；已合入分支必然有远端可达证据，未合入分支的 unpushed 留空且拒绝删除。
主仓库中的 .env 等 ignored 内容不阻断瘦身，它们始终保留；只有明确产物候选能被删。
符号链接产物只计链接自身的磁盘大小，显式清理也只 unlink；即使目标位于工作区外，目标内容不会被遍历或删除。
