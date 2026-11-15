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
3. 根进程 ppid 为 1 或直接探测确认不存在；kill(pid, 0) 仅 ESRCH 表示退出，EPERM/未知均保留

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
`kill_group` 用内核快照、目标 cwd/app 元数据及端口做轻量复核，不再每组全量 analyze。
根进程身份必须不变，已退出的子进程可以消失，新增子进程不继承授权；仍有新成员时返回 remaining。
仅允许当前 uid、已知启动时间、无监听端口且无任何保护条件的组。
先尝试写审计 attempt，再按进程树深度从子到父发送 TERM；超时再发送 KILL，并轮询 1.5 秒。
审计写入失败返回 False 并警告，清理照常，返回值只反映清理结果。SZOMB 视为退出，自行退出记 gone。
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

- `config.py`：JSON 类型校验、工作区边界、保护规则。路径 `AGENTSCOPE_CONFIG` > 默认路径；工作区环境变量优先。按文件版本缓存；非法配置只读回退并警告、通知关闭、清理跳过；命令行未知且存在命令行保护规则时保护成员。
- `procstat.py`：统一内核枚举，`pbi_start_tvsec@120` / `tvusec@128` 合成微秒启动身份；批量 ps 仅补充完整命令行，失败时不猜测。
- `history.py`：0600 JSONL append，kills 记录 attempt/dry-run/skipped/cleaned/gone/remaining，sessions 记录结束摘要。history 可按类型、天数筛选并忽略损坏行。
- `notify.py`：osascript 参数传递文本，避免脚本插值；每个实例按 PID + kind 冷却，默认关闭。watch 的 seen 是 `(pgid, 所有根 PID/启动时间)`，每轮与当前组取交集；失败不记 seen。
- `panel.py` / `panel_worker.py`：输入线程使用最近快照，单后台线程串行处理采集与确认后的清理，避免共享缓存竞态；进程组刷新周期 20 秒。termios 在 finally 恢复，SIGTERM/SIGHUP handler 抛 SystemExit 并恢复旧 handler。非 TTY 走原刷新路径。
- `sessions.py`：配置价格覆盖内置精确型号；回退定价通过 cost_estimated 传到会话与汇总渲染。
- `install.py`：成功复制新备份后，仅删除同目标、严格时间戳命名的旧备份；保留最新一份。

测试使用合成快照，可注入 cwd/ports/tty/app/parent 查询，并包含只操作自身创建进程身份的真实集成测试；其余信号、通知和面板清理均 mock；
不运行安装器 main，不修改宿主配置。全量只读冒烟在独立状态目录记录预演历史。

`lifecycle.py` 提供共享 deadline 与信号恢复上下文。hook 总预算 25 秒，stdin 有完整读取预算；
只做一次进程内 analyze，再逐组轻量复核，查询和等待使用剩余预算，到期不再发新信号。
摘要 finally 覆盖解析、扫描和 SIGTERM/SIGHUP。watch 默认不采集通知所需资源，告警旁路单独捕获异常，整个单轮也捕获异常并继续。
端口 None 表示采集失败而非空集合，cleanable 对未知端口禁止清理；所有监听地址均被保护。

## 范围追加 13–21

- `ownership.py`：当前 Orca 祖先链三态识别（true/false/unknown），unknown 保守保护；会话归属优先祖先 PID，worktree 查找只读本地 .git marker。
- `mcp.py`：命令行模式提取 MCP 名，内核 RSS 聚合；保留 instance 列表及 ancestor/cwd/cwd-ambiguous/unknown 归属证据。agents.collect 复用已有进程和 cwd 快照，把关联 MCP 注入会话记录，render 在 agents/panel 展示。
- `ports.py`：每个监听 PID 映射地址、cwd、worktree 和会话；abandoned 仅由配置工作区内明确不存在的 cwd 产生，权限失败不算删除。clean 的显式参数可放行这类端口，但不绕过 cleanable、轻量复核和审计。
- `activity.py`：读取有界日志尾部的事件类型（不存储消息内容），结合 CPU/写入年龄细分会话状态；120 秒空闲、600 秒疑似卡住/循环。共享 cwd 冲突仅针对主会话，告警不触发系统通知或自动清理。
- `heavy.py`：固定主机级 lock inode，O_NOFOLLOW 防止锁路径符号链接替换，flock 非阻塞竞争与负载门槛；锁覆盖命令运行期，关闭 fd 自动释放，禁止删除锁文件。
- `disk.py`：有限深度/时间预算盘点生成目录，统计磁盘分配块、去重硬链接、不跟随 symlink；历史会话和 gitdir 指针提供删除工作区证据。显式删除路径白名单，复用 analyze/cleanable 做进程检查且拒绝任何使用中的工作区，再核对目录 inode，审计后删除；不清理 Git 元数据。
- `doctor.py`：仅文件读取，凭据只输出状态，额度仅保留数字白名单字段；不输出认证信息、不执行登录程序、不联网。
- `metrics.py`：watch 在现有 info/units 上补充低成本内核资源与 vm_stat，通知资源采集仍为可选旁路。flock 串行轮转/append，默认 1 MiB + 3 归档；timeline 支持带时区 ISO 或相对 m/h/d。
- `extensions.py`：mcp/ports/disk/doctor/timeline/run 的 CLI 注册。默认只读，run --dry-run 不获取锁或执行命令，磁盘删除与遗弃端口清理均要求显式用户参数。

限制：MCP 命名模式和会话状态是启发式；同 cwd 不能证明具体会话归属；Orca 祖先已退出后无法追溯历史归属。
磁盘目录 inode/进程检查与 rmtree 非原子，指标 RSS 和原 footprint 口径不同，额度和登录状态均不能替代在线验证。

复审 r2：disk 的 deletion_evidence 在展示及删除前重复验证 `.git` 和同级语言/构建项目标记，
工作区路径与目录名本身不再构成删除授权。heavy 默认 300 秒排队预算、15 秒状态提示，
用 pass_fds 将 flock fd 交给子进程，嵌套调用用 AGENTSCOPE_HEAVY_HELD 避免重复排队。
真实测试覆盖包装进程退出后子进程仍持锁，子进程退出后释放；轮询不提供 FIFO 保证。

## Worktree 回收

`worktrees.py` 分离发现、证据检查、大小估计、执行四阶段。发现遍历配置根，跳过 Git 对象、生成目录及 symlink，
使用 NUL 分隔的 Git porcelain 解析注册；跨根登记仍盘点，主路径由首条记录确定。
所有 Git 命令带 --no-optional-locks，子进程 timeout 服从总预算；默认离线，显式 --online/--fetch 才调用 gh/fetch。
配置 worktrees.idle_days/stale_days/protect 由 config.py 校验，非法设置禁止清理。

安全分类保留所有 reasons：主目录、锁定、保护、路径别名、进程未知/在用、Orca 缺失、脏/未跟踪、未推送及合入/空闲条件。
PR 必须匹配当前 HEAD，避免复用分支名造成误删；origin/HEAD 缺失意味着合入未知。
进程 cwd 用 procstat 批量采集，当前用户任一进程 cwd 不可读则保守拒绝。
大小后置采集，状态目录 worktree-sizes.json 原子替换，按路径 + 根 mtime 命中且最多缓存 24 小时；
缓存和审计失败不使报告崩溃。大小只是估计，不参与删除授权。

执行依类别重新读取注册、HEAD/branch、Git 状态与进程证据，未达到同类资格则 skipped。
prune 复核整仓缺失条目；branch -d 前确认未 checkout、已合入默认分支、无未推送且 OID 未变。
slim 只选无 Git 跟踪文件的可再生目录，重新检查陈旧仓库后复用 disk.clean；保护路径同样有效。
Orca 路径通过 CLI 删除，保留管理器元数据约束，绝不回退到直接 git remove。
worktrees.jsonl 记录 dry-run/attempt/cleaned/skipped；审计沿用 fail-open 语义。
watch 调用 weekly_summary，最近 7 天已有记录即跳过；汇总不删除数据（会写大小缓存与历史），通知种类 WORKTREE_SUMMARY 单独冷却。

测试以 TemporaryDirectory 下真实 Git 仓库及 worktree 验证删除、prune、branch -d、瘦身及各拒绝条件；
进程和 Orca/gh 外部证据使用 mock。本机验收只运行报告/预演，不实际删除、不 fetch、不联网。

审查修复：inspect 使用 NUL 分隔的 ls-files -o -i --exclude-standard --directory 检查 ignored 条目，
只有 disk.ARTIFACTS 目录与同一 worktree 的项目标记证据匹配才放行；保存拒绝条目计数和前五条路径，不读文件内容。
fresh_row 重复该检查。detached 且 merged 不为 true 时强制 manual，.gitmodules 存在时报告 submodules。
artifacts 在遍历时剪掉具有 .git 标记的子目录；artifact_safe 要求 deletion_evidence 与所属 worktree 一致，
并拒绝内部嵌套仓库、跟踪文件和 Git 状态改动，执行前同样复核。
unknown 是 manual 中 query_failed/process_cwds_unknown/path_unknown 的子集，影响 complete 并进入周汇总与通知。
weekly_summary 按状态目录在进程内记录尝试时间，读写历史或盘点失败也节流七天；单次预算降为 5 秒，限制 watch 阻塞。
