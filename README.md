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

```bash
agentscope scan                # 列出Agent 会话与残留进程组
agentscope tree <pgid>         # 展开某个进程组看构成
agentscope clean --dry-run     # 预演清理
agentscope clean --yes         # 执行清理
agentscope watch --interval 60 # 守护模式（前台）
```

卸载：

```bash
python3 install/install.py --uninstall
```

## 工作原理

### 技术基石

三个内核行为，共同构成整套方案（已在 macOS 26.6 / arm64 实测验证）：

1. 子进程**继承父进程的进程组**（pgid）
2. 父进程退出后，子进程成为孤儿（ppid=1）但**保留原 pgid**
3. `killpg(pgid)` 可一次性终止整组残留进程

因此按 pgid 聚合，就能把"某次 Agent 会话残留的全部进程"识别为一个可清理单元。

### 关键判据：如何区分「残留」与「健康」

**最容易踩的坑**：`ppid=1` 并不代表残留。

macOS 上所有桌面应用（Arc、Claude、ChatGPT、Lark、Logseq、Paseo…）都是
`launchd` 拉起后 reparent 到 init 的，根进程 ppid 同样是 1，但它们完全健康。
早期版本按 ppid 判定，差点把整屏桌面应用当垃圾清掉。

真正的判据是**两条同时成立**：

| 条件 | 说明 |
|---|---|
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

### killpg 在受限环境会失败

沙箱不允许影响其它进程组，`killpg` 返回 `EPERM`。此时降级为逐进程 kill，
且必须**先子后父**（父进程先死会留下孤儿）。

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
