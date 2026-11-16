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

生成浅色与深色 PNG，像素倍率为 2。渲染程序没有 socket、清理、登录项或终端调用。ImageRenderer 不支持原生菜单及滚动容器，因此预览以相同会话内容和相同可视区域替换滚动容器，以齿轮图标替换原生 Menu；不模拟菜单展开或滚动交互。上下文条使用 SwiftUI 图形绘制，应用和预览共享实现。

## 数据语义

- `sessions` 以日志 ID/路径和进程祖先归并，未匹配日志时使用 `pid:<root>`；目录相同不是归并依据。内存和 CPU 累加，成本与 token 只保留一次。
- 嵌入在 ChatGPT 中的 Codex 从已识别子进程向上识别宿主；同一桌面宿主作为一行资源会话。无法可靠区分同一桌面应用内部的多个聊天标签页。
- `project` 使用 git 顶层目录名，失败时使用 cwd 末级名称。`processes` 包含归并进程数。
- `context_pct` 是剩余百分比；没有明确窗口大小时为 null。Claude 可通过现有 `--context-limit` 显式指定窗口，默认不再假定为 200k。
- 告警以类别和级别汇总，`count` 与 `pids` 表示受影响的不同会话及其代表 PID，保留 `pid`、`msg` 等旧字段。
- 现有 `totals.cost_usd` 是会话成本，不能视为今日消费。UI 支持可选 `totals.today_cost_usd`，当前生产端未提供可靠的日界统计，因此概览显示“今日 —”；单行继续显示已有会话成本。缺失值不当成零。

契约 fixture 将旧示例中的省略号及状态枚举占位符改为可渲染的示例值，覆盖共享目录、未知上下文、不同工作状态和聚合进程数。
