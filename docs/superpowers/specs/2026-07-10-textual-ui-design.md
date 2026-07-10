# PaiCLI Textual UI 修复设计

## 目标

以 Textual 作为唯一交互界面，修复 Windows Terminal / PowerShell 7 中的显示与输入问题，并恢复完整交互能力。视觉采用 Aurora Console：深蓝底、酸性绿主操作色、青色回答、紫色思考、黄/红审批和错误。

## 架构

- `start_repl()` 只组装 Agent、配置和注册表，然后启动 `PaiCliApp`。
- 将 TUI 拆分为应用编排、聊天时间线、输入历史、计划审查、审批弹窗和状态栏等独立组件。
- 聊天区滚动，状态栏与输入区固定；挂载后输入框立即获取焦点。
- 为 80×24 优化；窗口变窄时隐藏次要统计并使用 ASCII 状态符号。目标是 Windows Terminal / PowerShell 7，不支持旧版 Windows PowerShell。

## 交互

- Enter 发送，Shift+Enter 换行；运行中禁用输入；`Ctrl+C` 优先取消当前运行，空闲时退出。
- 提示词历史持久化到 `~/.paicli/history/`，用 Up/Down 浏览；Tab 补全 slash 命令。
- 文本与思考增量实时刷新；最终回答展开，思考与成功工具调用默认折叠，错误自动展开。工具输出在卡片中可滚动，不截断。
- `/plan` 使用原生计划审查界面，提供执行、补充与取消操作；危险工具调用使用原生审批面板，不再使用 Rich/Prompt 的阻塞输入。
- 所有 slash 命令复用现有业务逻辑但通过 Textual 异步任务执行，避免阻塞 UI 主线程。

## 验证

- 先为焦点、实时增量、折叠卡片、历史、补全、计划和审批编写 Textual 回归测试，并由 Fake Agent 驱动真实事件序列。
- 在 80×24、窄窗口和 Windows 终端能力配置下执行布局 smoke test。
- 通过 Windows PTY 启动 smoke test 验证显示和直接输入。
- 配置 pytest 使用项目内可写的临时目录，以便在 Windows 环境运行全量测试。

## 验收标准

- `paicli` 启动后可直接输入。
- 生成过程实时可见。
- 已支持的 slash 命令、计划与审批均不离开 TUI。
- 测试、静态检查和 Windows PTY smoke test 通过。
