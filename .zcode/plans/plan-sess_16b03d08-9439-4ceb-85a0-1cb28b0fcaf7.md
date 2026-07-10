# 方案：引入 Textual 实现可折叠工具结果（鼠标点击展开/折叠）

## 背景

pico 项目使用 Textual 框架，其内置的 `Collapsible` widget 原生支持鼠标点击展开/折叠，零自定义代码。PaiCLI 当前使用 Rich 做静态渲染，无鼠标交互能力。本方案将渲染层从 Rich 迁移到 Textual，获得原生鼠标支持。

## 核心改动

### 1. 新增依赖
- `pyproject.toml` 添加 `textual>=0.50`

### 2. 新建 `src/paicli/render/textual_renderer.py`
Textual App 替代 RichRenderer，包含：

**Widgets:**
- `ToolCard(Static)`: 包装 `textual.widgets.Collapsible`，显示工具调用结果
  - 成功时自动折叠（`collapsed=True`）
  - 错误时保持展开（`collapsed=False`）
  - 输出限制 `max-height: 14` 行
  - 标题格式：`[OK] ⚡ 执行命令("ls -la")` / `[ERR] read_file("path")`
- `ChatLog(VerticalScroll)`: 消息滚动容器，mount ToolCard/AssistantMessage/UserMessage
- `InputBar`: 用户输入框（替代 prompt_toolkit）
- `StatusBar`: 底部状态栏（模型/Token/费用/时间）

**事件处理:**
- `handle(event)` 方法接收 agent 事件，通过 `call_from_thread` 安全更新 widget
- `tool_call` → 创建 ToolCard（running 状态）
- `tool_result` → 更新 ToolCard（success/error 状态，自动折叠/展开）
- `text_delta` → 追加到当前 AssistantMessage
- `thinking_delta` → 追加到思考面板

### 3. 新建 `src/paicli/render/tui_app.py`
Textual App 主类：
```python
class PaiCliApp(App):
    CSS = """
    ToolCard { border: tall #273244; }
    ToolCard .tool-output { max-height: 14; }
    """
    
    def compose(self):
        yield ChatLog(id="chat-log")
        yield InputBar(id="input-bar")
        yield StatusBar(id="status-bar")
    
    async def on_input_submitted(self, event):
        message = event.value
        event.input.clear()
        await self.run_agent(message)
    
    async def run_agent(self, message):
        async for event in self.agent.run(message):
            self.renderer.handle(event)
```

### 4. 修改 `src/paicli/entrypoints/repl.py`
- 移除 prompt_toolkit 的 `PromptSession` 和相关键盘绑定
- 启动 Textual App 替代原来的 REPL 循环
- 保留 slash command 处理逻辑（迁移到 Textual 的 key bindings）
- 保留 plan review 的交互逻辑（Ctrl+O/ESC 等快捷键迁移到 Textual BINDINGS）

### 5. 保留 `rich_renderer.py` 作为 fallback
- 非交互模式（pipe/redirect）继续使用 RichRenderer
- `PlainRenderer` 保持不变

### 6. 更新测试
- `tests/test_render.py` 中 RichRenderer 相关测试保留（fallback 场景）
- 新增 `tests/test_textual_renderer.py` 测试 Textual 渲染逻辑

## 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `pyproject.toml` | 修改 | 添加 textual 依赖 |
| `src/paicli/render/textual_renderer.py` | 新建 | Textual 版渲染器（核心） |
| `src/paicli/render/tui_app.py` | 新建 | Textual App 主类 |
| `src/paicli/render/__init__.py` | 修改 | 导出 TextualRenderer |
| `src/paicli/entrypoints/repl.py` | 修改 | 启动 Textual App |
| `tests/test_textual_renderer.py` | 新建 | Textual 渲染测试 |

## 架构优势

1. **鼠标点击原生支持**: Textual Collapsible 内置，零自定义代码
2. **Rich 组件复用**: Panel/Markdown/Table 等 Rich renderable 可直接嵌入 Textual widget
3. **核心逻辑不变**: Agent/Tool/MCP/Snapshot 等完全不受影响
4. **渐进式迁移**: RichRenderer 保留作为 fallback，可逐步切换
5. **更好的 UX**: 滚动、焦点、键盘导航、主题等 Textual 原生能力