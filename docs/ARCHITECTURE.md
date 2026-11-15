# 架构与技术细节

本文记录 agentscope 的设计决策与实现要点，尤其是那些**踩过坑之后才确定**的部分。

## 模块划分

```
bin/agentscope              主 CLI
├── agentscope_lib.procstat 唯一 libproc / cwd / 端口采集实现
├── 进程组聚合              按 pgid 组织进程
├── 残留判别                orphan / agent_like / has_live_session
├── 三重安全阀protected / live_session / 端口占用
└── agentscope_lib.cleanup  分类、保护、审计与逐成员清理入口

bin/agentscope-hook         Claude Code SessionEnd 钩子
├── 祖先链精确匹配          优先用 pgid 定位本次会话
└── 工作目录匹配（退化路径）  祖先链拿不到时用cwd 兜底

bin/codex-notify-wrapper.sh Codex notify 包装器
└── 转发原始 notify + 调用 hook

install/install.py          一键安装 / 卸载
install/exclude_index.py    批量排除 Spotlight 索引
```

## 三个内核行为

整套方案建立在三个可验证的内核行为之上：

| 行为 | 验证方式 | 结论 |
|---|---|---|
| 子进程继承父进程 pgid | 启动 bash + node，观察 pgid | 相同 |
| 父退出后子进程保留 pgid | 杀父进程，观察子进程 ppid | ppid=1 但 pgid 不变 |
| killpg 一次清整组 | 对测试组 killpg | 全部消失 |

## 判别残留 vs 健康：走过的弯路

### 弯路一：按 ppid == 1 判定

最初认为「父进程消失 = 残留」，于是 `ppid == 1` 作为孤儿判据。

**问题**：macOS 上所有桌面应用都是 `launchd` 拉起后 reparent 到 init 的，
根进程 ppid 同样是 1。实测发现 Arc、Claude、ChatGPT、Lark、Logseq、Paseo
全部被误判成可清理对象。

### 弯路二：按进程名匹配

改用「进程名含 claude/codex/node 等关键词」。

**问题**：桌面应用的 Helper 进程（`Browser Helper`、`ego Helper`）、
Electron 的 `(Renderer)` 也被误伤。

### 最终判据

所有根节点满足三条条件才算残留：

1. 根进程自身是 shell / 开发工具：`bash` `zsh` `fish` `sh` `node` `python3` `head` `tail` `deno` `bun`
2. 根进程的 **cwd 落在工作区目录内**
3. 根进程 ppid 为 1 或父进程不在快照中；活父进程即使不同 pgid 也不能清理

桌面应用的 cwd 是 `/` 或 `.app` 内部路径，天然不满足条件。

```
WORKSPACE_ROOTS = (
    ~/Workspace, ~/Worksapce, ~/Workbuddy,        # 注意：本机存在拼写错误目录
    ~/orca/workspaces, ~/.codex/worktrees, ~/.claude,
    ~/Desktop, ~/Documents, ~/Projects,
)
```

### 活跃度判定：为什么不用 CPU 时间

最初想用 CPU 时间增量判断会话是否活跃（跑两个采样点对比）。

**问题**：`proc_pidinfo` flavor 2 的 timeval 字段解析不可靠——
实测忙进程跑满 2 秒 CPU，解析出的数值几乎没变化；`proc_pid_rusage`
在受限环境下直接被拒。

**改用**「进程名 + tty 持有」：

```bash
lsof -c claude -c node -d 0,1,2
```

交互式 Agent 会话一定持有终端设备。这比 CPU 时间可靠，也不需要额外系统调用。

## 性能：24.5s → 1.06s

hook 的timeout 是 30 秒，而逐进程调`lsof` 取 cwd 在 500+ 进程时需要 **24.5 秒**，
几乎必然超时。

用 cProfile 定位到 `proc_cwd` 被调用 448 次、每次 56ms。

**解法**：批量调用

```bash
lsof -a -d cwd -F pn -p <pid1,pid2,...>
```

`-F pn` 的输出格式是：

```
p262
fcwd
n/
```

即 `p<pid>` 后跟 `n<path>`。**不能用默认表格格式解析**（那需要按列位置切分，
路径含空格会错位）。

## 清理与进程身份

`bin/agentscope` 仅保留包路径引导与 CLI 入口；实现集中在 `cleanup.py`。
`kill_group` 重新分析快照，要求候选成员 PID + 内核启动时间与确认时相同。
仅允许当前 uid、已知启动时间、无监听端口且无任何保护条件的组。
先写审计 attempt，再按进程树深度从子到父发送 TERM；超时再发送 KILL。
每次信号前核对 PID、启动时间和 pgid；不向新加入的成员广播 killpg。
检查与信号之间仍有无法用标准库完全消除的竞态窗口。

## 三重安全阀

| 阀门 | 判据 | 防止什么 |
|---|---|---|
| Agent 本体检测 | 组内有 claude/codex → 跳过 | 杀掉正在进行的会话 |
| 端口占用 | 组内有监听端口 → 跳过 | 杀掉你要用的服务 |
| 终止前二次校验 | 重新读组成员，发现 Agent 本体 → 放弃 | 扫描到清理之间的竞态 |

此外保护：当前会话自己的进程树（通过祖先 pgid 识别）、桌面应用宿主
（进程名 + `lsof` 反查 `.app` 路径 + cwd 位于 `/Applications`）。

## 祖先链匹配：hook 的精确路径

Claude Code 启动 hook 时，hook 是它的子进程，**共享同一个 pgid**。
因此可以直接从祖先链取到本次会话的 pgid：

```python
pid = os.getpid()
while pid > 1:
    pgids.add(os.getpgid(pid))
    pid = ppid_of(pid)
```

这比按 cwd 猜测精确得多 —— 同一个目录下可能有多个并发会话，
按 cwd 匹配会连带清理别人的东西。

祖先链拿不到时，才退化为工作目录匹配。

## 环境限制备忘

本项目在开发环境遇到的一些限制，以及对应的绕法：

| 限制 | 表现 | 绕法 |
|---|---|---|
| `ps` / `top` 被禁 | `PermissionError: [Errno 1]` | libproc `proc_listallpids` + `proc_pidinfo` |
| 信号 EPERM | 进程权限限制 | 保留 remaining 审计结果 |
| `launchctl bootstrap` errno 5 | 无法立即加载 launchd agent | `launchctl enable` + 登录时自动加载 |
| Python `open()` 被拦 | 写入路径被当成文件名 | 用 Write 工具写文件 |
| `os.unlink` 被拦 | 转 trash broker，某些路径抛 errno 66 | 用 `/bin/rm` 子进程 |
| `du` 参数过多 | 静默返回空 | 分批（每批 100 个） |
| `find` 表达式陷阱 | `\( -o -name x \)` 匹配所有目录 | 括号内必须以 `-name` 开头 |

## `proc_pidinfo` flavor 3 字段偏移

```python
pbi_flags  @0     pbi_status @4     pbi_xstatus @8
pbi_pid    @12    pbi_ppid   @16    pbi_uid    @20
pbi_gid    @24    pbi_ruid   @28    pbi_rgid   @32
pbi_svuid  @36    pbi_svgid  @40
pbi_comm   @48  (16 bytes)   pbi_name @64 (32 bytes)
```

`pbi_comm` 是进程名（截断到16 字节），`pbi_name` 是完整路径名。

## Hardening 模块与状态

- `config.py`：JSON 类型校验、工作区边界、保护规则。路径 `AGENTSCOPE_CONFIG` > 默认路径；工作区环境变量优先。非法配置失败关闭；命令行未知且存在命令行保护规则时保护成员。
- `procstat.py`：统一内核枚举，`pbi_start_tvsec@120` / `tvusec@128` 合成微秒启动身份；批量 ps 仅补充完整命令行，失败时不猜测。
- `history.py`：0600 JSONL append，kills 记录 attempt/dry-run/skipped/cleaned/gone/remaining，sessions 记录结束摘要。history 可按类型、天数筛选并忽略损坏行。
- `notify.py`：osascript 参数传递文本，避免脚本插值；每个实例按 PID + kind 冷却，默认关闭。watch 的 seen 是 `(pgid, 所有根 PID/启动时间)`，每轮与当前组取交集；失败不记 seen。
- `panel.py`：termios raw 模式，finally 恢复终端；选择通过组身份保持，确认绑定成员快照。非 TTY 走原刷新路径。
- `sessions.py`：配置价格覆盖内置精确型号；回退定价通过 cost_estimated 传到会话与汇总渲染。
- `install.py`：成功复制新备份后，仅删除同目标、严格时间戳命名的旧备份；保留最新一份。

测试使用合成进程快照，可注入 cwd/ports/tty/app 查询。所有信号、通知和面板清理均 mock；
不运行安装器 main，不修改宿主配置。全量只读冒烟在独立状态目录记录预演历史。
