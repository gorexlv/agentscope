# AgentscopeBar

macOS 14+ 的 SwiftUI 菜单栏面板。窗口宽 340pt，最高 560pt；头部、概览和维护操作固定，会话区域滚动。设置菜单保留刷新间隔、登录项、终端面板和退出操作。清理仍须先向本地 daemon 请求预演，再由用户确认一次性令牌。

## 构建与测试

```sh
swift build --package-path macos/AgentscopeBar
swift test --package-path macos/AgentscopeBar
bash macos/build-app.sh
```

构建脚本只生成本地 app bundle，不安装或启动它。

## 离线预览

```sh
swift run --package-path macos/AgentscopeBar AgentscopePreview \
  macos/AgentscopeBar/Tests/AgentscopeCoreTests/Fixtures/snapshot.json \
  '/tmp/preview-{appearance}-fixture' online
bin/agentscope snapshot --json > /tmp/agentscope-real.json
swift run --package-path macos/AgentscopeBar AgentscopePreview \
  /tmp/agentscope-real.json '/tmp/preview-{appearance}-real' offline
```

生成浅色与深色 PNG，像素倍率为 2。渲染程序没有 socket、清理、登录项或终端调用。ImageRenderer 不支持原生菜单及滚动容器，因此预览以相同会话内容和自适应完整内容高度替换滚动容器，以齿轮图标替换原生 Menu；不模拟菜单展开或滚动交互。上下文条使用 SwiftUI 图形绘制，应用和预览共享实现。

## 数据语义

- `sessions` 以日志 ID/路径归并；只有明确辅助进程才沿直接父子关系归并，独立 CLI（包括嵌套 CLI）保留自己的行。真实 `session_id` 保持原义，未匹配日志时为 null；新增 `session_key` 提供日志路径或 `pid:<root>` 回退键。内存和 CPU 累加，成本与 token 只保留一次。
- 桌面宿主接纳明确的直接辅助进程；无日志且 cwd 为根目录或空的 crashpad / app-server，在所属应用 bundle 唯一时计入宿主辅助进程。独立 CLI 各自成行，宿主外无法归属的无日志辅助进程不列为会话，目录相同不是归并依据。
- `project` 使用 git 顶层目录名，失败时使用 cwd 末级名称。`processes` 包含归并进程数，`auxiliary_processes` 标记其中的辅助进程数量。
- `context_pct` 是剩余百分比；Claude 优先使用显式 `--context-limit`，否则按模型推断：`[1m]` 或已知 1M 模型使用 1,000,000，其余已识别 Claude 模型使用 200,000，模型或用量未知时为 null。模型表依据 [Anthropic 上下文窗口说明](https://platform.claude.com/docs/en/build-with-claude/context-windows)。
- 告警以类别和级别汇总，`count` 与 `pids` 表示受影响的不同会话及其代表 PID，保留 `pid`、`msg` 等旧字段，冲突汇总新增 `cwds`。通知保留逐会话明细，以 `(session_id, kind)` 去重（缺失 ID 时回退到 `session_key`）。根目录与主目录不参与目录冲突告警。
- 现有 `totals.cost_usd` 是会话成本，不能视为今日消费。UI 支持可选 `totals.today_cost_usd`，当前生产端未提供可靠的日界统计，因此概览改为显示已知会话成本合计，全无成本时显示“成本未知”；单行继续显示已有会话成本。缺失值不当成零。

契约 fixture 将旧示例中的省略号及状态枚举占位符改为可渲染的示例值，覆盖共享目录、未知上下文、不同工作状态和聚合进程数。

仅当多个独立进程共享同一 cwd 时，日志消歧才排除超过两小时未更新的候选；单个 Claude 会话保留原有匹配方式，长时间空闲仍可触发 IDLE。每个独立进程只能认领一个未占用日志，有进程启动时间和文件创建时间时按接近程度配对。这是启发式归属，不代替明确的进程到日志绑定。

后续改进：为慢速目录的 git 顶层查询增加有失效机制的缓存；增强恢复会话的显式日志绑定；补充原生滚动、设置、Finder 和 VoiceOver 实机验收。

## 桌面浮窗

在菜单栏面板的齿轮菜单中开启“桌面浮窗”，再通过“浮窗尺寸”选择小或中；默认关闭。小尺寸为 280×184pt，显示负载、内存和告警数；中尺寸为 340×440pt，增加会话数与已知成本、最多三个会话、可回收空间、更新时间和连接状态。浮窗复用菜单栏卡片与会话行，直接观察同一个模型，沿用同一刷新间隔，不创建额外轮询。

浮窗只读，没有清理、Finder 或终端按钮；点击任意位置打开菜单栏面板，拖动可移动。开关、尺寸和位置保存在 UserDefaults。切换尺寸保持左上角位置，显示器移除或分辨率变化后会把窗口夹回可见区域；异常小的显示区域会缩小显示内容，显示器暂时不可用时保留已有位置。

窗口使用不激活的无边框 NSPanel，层级为桌面图标层加一，低于普通应用窗口；加入所有空间，以 stationary / ignoresCycle 排除普通 Mission Control 窗口排列与窗口轮换。应用保持 accessory 模式，不新增 Dock 或 ⌘Tab 项。菜单栏改用 NSStatusItem + NSPopover 承载原有 SwiftUI 内容，使浮窗点击通过公开 AppKit 接口打开同一个面板；不依赖 WidgetKit、App Group 或付费签名。

渲染小/中 × 浅/深四张真实数据预览：

```sh
bin/agentscope snapshot --json > /tmp/agentscope-real.json
swift run --package-path macos/AgentscopeBar AgentscopePreview \
  /tmp/agentscope-real.json '/tmp/preview-panel-{size}-{appearance}' offline desktop
```

预览只绘制共享内容，不创建或显示 NSPanel，不修改 UserDefaults。尺寸切换和跨屏位置恢复有纯逻辑测试；窗口层级、拖动、点击弹出、空间切换以及 VoiceOver 的真实交互仍需运行应用后验收。
