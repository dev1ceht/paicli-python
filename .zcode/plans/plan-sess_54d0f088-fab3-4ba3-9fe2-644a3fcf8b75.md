## UI 问题诊断报告与修复计划

经过对 `src/paicli/render/` 和 `src/paicli/entrypoints/` 的全面分析，发现以下影响终端显示的问题：

---

### 🔴 严重问题（导致功能异常/崩溃）

#### 问题 1：TUI 状态栏 Token/Cost 数据永远为 0
- **文件**：`src/paicli/render/tui_app.py:502-506`
- **原因**：`_record_run_summary()` 读取 `event.get("usage")`，但 `query.py:143-148` 发出的 `done` 事件结构是 `{"type": "done", "total_tokens": ..., "total_turns": ...}`，没有 `usage` 子键
- **对比**：`rich_renderer.py:720-738` 的 `_record_run_summary()` 正确地直接读取 `event.get("total_tokens")`
- **影响**：StatusBar 永远无法显示正确的 token 用量和费用
- **修复**：对齐 `tui_app.py` 的事件读取逻辑与 `query.py` 的实际事件结构

#### 问题 2：TUI 中 MCP 命令会抛出 RuntimeError
- **文件**：`src/paicli/render/tui_app.py:696-713`
- **原因**：在 Textual 的异步事件循环中调用 `asyncio.get_event_loop().run_until_complete()`，无法嵌套事件循环
- **影响**：`/mcp enable` 和 `/mcp restart` 命令直接崩溃
- **修复**：改为使用 `await` 异步调用

#### 问题 3：Rich Renderer 双 Live 实例冲突
- **文件**：`src/paicli/render/rich_renderer.py`
- **原因**：`_live`（流式 Markdown）和 `_thinking_live`（思考过程）是两个 `rich.live.Live` 对象共享同一个 `Console`。Rich 官方只支持每个 Console 一个活跃 Live
- **影响**：当 thinking 和 text delta 交替出现时，显示会闪烁/错乱
- **修复**：合并为单个 Live 实例，或确保两个 Live 不会同时活跃

---

### 🟡 中等问题（功能降级）

#### 问题 4：TUI 的 Diff 渲染质量低
- **文件**：`src/paicli/render/tui_app.py:424-444`
- **原因**：简单地将所有 before 行标为删除、after 行标为添加，未使用 LCS diff 算法
- **对比**：`rich_renderer.py:606-643` 使用完整的 LCS diff + 上下文行
- **修复**：复用 `rich_renderer.py` 的 `_diff_ops` / `_group_hunks` 逻辑

#### 问题 5：TUI 模式无启动 Banner
- **文件**：`src/paicli/render/tui_app.py` 的 `on_mount()`
- **原因**：TUI 的 `on_mount()` 只设置了标题和状态栏，没有显示 ASCII Logo、模型信息、工作区等启动信息
- **对比**：`RichRenderer.banner()` 提供完整的启动信息
- **修复**：在 `on_mount()` 或 `ChatLog` 中添加等效的 Banner 信息

#### 问题 6：NO_COLOR 环境变量无效
- **文件**：`src/paicli/render/rich_renderer.py:21`
- **原因**：`_NO_COLOR` 变量被计算但从未传给 `Console()` 构造函数
- **修复**：`Console(no_color=_NO_COLOR)`

---

### 🟢 代码质量问题（不影响显示但需清理）

#### 问题 7：大量重复代码
- `_TOOL_LABELS` + `_tool_label()` 在 `rich_renderer.py` 和 `textual_widgets.py` 中重复
- `format_tokens`/`format_elapsed`/`format_cost` 在两个文件中重复
- `_shorten_home` 在 `repl.py` 和 `rich_renderer.py` 中重复
- Slash 命令逻辑在 `tui_app.py` 和 `repl.py` 中重复
- **修复**：提取共享模块 `render/_common.py`

#### 问题 8：repl.py 中的死代码
- `_run_agent`、`_run_plan_agent`、`_handle_slash`、`_prompt_message`、`_bottom_toolbar` 等函数在 TUI 模式下不再被调用
- `PromptSession` 在 `repl.py:367` 处使用但从未导入（死代码中的 NameError）
- **修复**：移除或标注为 legacy

#### 问题 9：PlainRenderer 未被使用
- 被导出但从未实例化，`render_mode="plain"` 配置选项存在但从未被选择
- **修复**：在 `--plain` 或管道模式下正确选用

---

### 建议修复优先级

| 优先级 | 问题 | 工作量 |
|--------|------|--------|
| P0 | 问题 1：TUI Token/Cost 显示为 0 | 小 |
| P0 | 问题 2：MCP 命令崩溃 | 小 |
| P1 | 问题 3：双 Live 冲突 | 中 |
| P1 | 问题 4：TUI Diff 质量 | 中 |
| P2 | 问题 5：缺少 Banner | 小 |
| P2 | 问题 6：NO_COLOR 无效 | 小 |
| P3 | 问题 7-9：重复代码和死代码 | 大 |

是否要我按照此优先级逐一修复这些问题？