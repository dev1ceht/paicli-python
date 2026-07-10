# Textual UI Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a Windows Terminal / PowerShell 7-first Aurora Console Textual UI that starts focused, streams output, preserves complete interactive commands, and performs plan review and approval natively.

**Architecture:** Keep `PaiCliApp` as the only interactive surface. Move input history, event-to-widget rendering, plan review, and approval into small Textual-native units; `start_repl()` only constructs domain services and injects callback adapters.

**Tech Stack:** Python 3.11+, Textual, Rich renderables, pytest, Windows Terminal / PowerShell 7.

## Global Constraints

- Textual is the only interactive renderer; do not restore a Rich/prompt-toolkit fallback REPL.
- Target Windows Terminal / PowerShell 7 and 80×24 terminals; use ASCII for functional status indicators.
- Use Aurora Console colors: `#0d1117` background, `#a8ff60` primary, `#60d8ff` assistant, `#c084fc` thinking, `#facc15` warning, `#ff4d5a` error.
- Enter sends, Shift+Enter inserts a newline, Ctrl+C cancels an active run and exits only when idle.
- Successful tool calls and thinking are collapsed by default; errors remain expanded; no tool output truncation.
- New production behavior must be introduced test-first.

---

## File Structure

- Create `src/paicli/render/history.py`: bounded, UTF-8 line-based prompt history with cursor navigation.
- Create `src/paicli/render/tui_events.py`: maps Agent and Plan executor dictionaries to typed Textual messages.
- Create `src/paicli/render/tui_dialogs.py`: native approval and plan-review modal screens returning futures.
- Modify `src/paicli/render/textual_widgets.py`: Aurora widgets, markup-safe information blocks, streaming message cards, command-aware multiline input, scrollable tool cards.
- Modify `src/paicli/render/tui_app.py`: app orchestration, focus, history, event routing, cancel semantics, native slash / plan interaction.
- Modify `src/paicli/entrypoints/repl.py`: inject native plan and approval callbacks; remove obsolete blocking REPL-only dependencies.
- Create `tests/test_tui.py`: async Textual `run_test` regression coverage.
- Modify `pyproject.toml`: make pytest use `testing/pytest-basetemp` through an opt-in test fixture/config helper only if the existing sandbox still cannot create the system temporary directory.

### Task 1: Define and lock the TUI regression contract

**Files:**
- Create: `tests/test_tui.py`
- Modify: `src/paicli/render/tui_app.py`

**Interfaces:**
- Consumes: `PaiCliApp(agent, config, cwd, registry, mcp_manager)`.
- Produces: protected, testable app behavior: mounted `Input` is focused; `handle_event()` renders deltas before terminal events; `action_interrupt()` cancels active work.

- [ ] **Step 1: Write the failing focus and streaming tests**

```python
import asyncio

from paicli.config import PaiCliConfig
from paicli.render import PaiCliApp
from textual.widgets import Input


class FakeAgent:
    async def run(self, _message: str):
        yield {"type": "text_delta", "text": "hello"}
        yield {"type": "done", "total_turns": 1, "total_tokens": 1}


def test_mount_focuses_the_message_input():
    async def scenario():
        app = PaiCliApp(config=PaiCliConfig(), cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert isinstance(app.focused, Input)
            app.exit()
    asyncio.run(scenario())


def test_text_delta_creates_a_visible_streaming_message_before_done():
    async def scenario():
        app = PaiCliApp(agent=FakeAgent(), config=PaiCliConfig(), cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            app.handle_event({"type": "text_delta", "text": "hello"})
            await pilot.pause()
            assert "hello" in app.query_one("#chat-log").renderable_text()
            app.exit()
    asyncio.run(scenario())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_tui.py -q --basetemp testing/pytest-basetemp`

Expected: FAIL because the current app focuses `ChatLog` and buffers text deltas without a rendered streaming message.

- [ ] **Step 3: Add minimal test seams to the app and chat log**

```python
# ChatLog
def renderable_text(self) -> str:
    return "\n".join(str(widget.render()) for widget in self.children)

# PaiCliApp.on_mount
self.query_one(Input).focus()
```

Do not implement the full streaming widget in this task; only add the stable `renderable_text()` seam and initial focus needed by the two tests.

- [ ] **Step 4: Run the focused tests**

Run: `python -m pytest tests/test_tui.py::test_mount_focuses_the_message_input -q --basetemp testing/pytest-basetemp`

Expected: focus test PASS; streaming test remains red until Task 3.

- [ ] **Step 5: Commit the contract**

```bash
git add tests/test_tui.py src/paicli/render/tui_app.py src/paicli/render/textual_widgets.py
git commit -m "test: cover textual startup focus"
```

### Task 2: Build Aurora widgets and persistent multiline input

**Files:**
- Create: `src/paicli/render/history.py`
- Modify: `src/paicli/render/textual_widgets.py`
- Modify: `tests/test_tui.py`

**Interfaces:**
- Produces `PromptHistory(path: Path, limit: int = 200)` with `append(text: str)`, `previous() -> str`, `next() -> str`, and `reset_cursor()`.
- Produces `CommandInput(Input)` which posts `MessageSubmitted(value: str)` on Enter and inserts `"\n"` on Shift+Enter.

- [ ] **Step 1: Write failing history and markup tests**

```python
from paicli.render.history import PromptHistory
from paicli.render.textual_widgets import ChatLog


def test_prompt_history_round_trips_utf8_messages(tmp_path):
    history = PromptHistory(tmp_path / "prompt_history.txt")
    history.append("解释 Textual\n的输入行为")
    assert history.previous() == "解释 Textual\n的输入行为"
    assert history.next() == ""


def test_info_markup_is_rendered_not_shown_as_literal_tags():
    log = ChatLog()
    log.add_info("[red]Error:[/red] failed")
    assert "[red]" not in log.renderable_text()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_tui.py -q --basetemp testing/pytest-basetemp`

Expected: import failure for `PromptHistory`; markup assertion fails because `Text()` preserves literal tags.

- [ ] **Step 3: Implement the small focused units**

```python
# history.py
class PromptHistory:
    def __init__(self, path: Path, limit: int = 200) -> None:
        self.path, self.limit, self._items, self._cursor = path, limit, [], 0
        if path.exists():
            self._items = [line.rstrip("\n") for line in path.read_text(encoding="utf-8").splitlines() if line]
        self._items = self._items[-limit:]
        self._cursor = len(self._items)

    def append(self, text: str) -> None:
        if text and (not self._items or self._items[-1] != text):
            self._items = (self._items + [text])[-self.limit:]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("\n".join(self._items) + "\n", encoding="utf-8")
        self.reset_cursor()

    def previous(self) -> str:
        self._cursor = max(0, self._cursor - 1)
        return self._items[self._cursor] if self._items else ""

    def next(self) -> str:
        self._cursor = min(len(self._items), self._cursor + 1)
        return self._items[self._cursor] if self._cursor < len(self._items) else ""

    def reset_cursor(self) -> None:
        self._cursor = len(self._items)

# textual_widgets.py
from rich.markup import escape

def add_info(self, text: str, *, style: str = "dim") -> None:
    self.mount(Static(Markdown(text) if "[" in text else Text(text, style=style)))
```

Apply the Aurora palette to `DEFAULT_CSS`; use `+`, `!`, `>` and `*` in functional labels, with color as enhancement only.

- [ ] **Step 4: Run widget and history tests**

Run: `python -m pytest tests/test_tui.py -q --basetemp testing/pytest-basetemp`

Expected: PASS for history, markup, and focus tests.

- [ ] **Step 5: Commit the input foundation**

```bash
git add src/paicli/render/history.py src/paicli/render/textual_widgets.py tests/test_tui.py
git commit -m "feat: add textual history and aurora widgets"
```

### Task 3: Render Agent events live with expandable cards

**Files:**
- Create: `src/paicli/render/tui_events.py`
- Modify: `src/paicli/render/textual_widgets.py`
- Modify: `src/paicli/render/tui_app.py`
- Modify: `tests/test_tui.py`

**Interfaces:**
- Produces `UiEvent.from_agent(event: dict[str, Any]) -> UiEvent` with `kind`, `payload`, and optional `task_id`.
- Produces `ChatLog.begin_stream(role: str) -> StreamingMessage`, `StreamingMessage.append(text: str)`, and `StreamingMessage.finish(collapsed: bool = False)`.
- `PaiCliApp.handle_event()` delegates all Agent, plan, task, and error dictionaries through `UiEvent`.

- [ ] **Step 1: Write failing live-card tests**

```python
def test_tool_error_card_stays_expanded_and_retains_full_result():
    async def scenario():
        app = PaiCliApp(config=PaiCliConfig(), cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            result = "x" * 5000
            app.handle_event({"type": "tool_call", "name": "read_file", "input": {"path": "a.py"}})
            app.handle_event({"type": "tool_result", "name": "read_file", "result": result, "is_error": True})
            await pilot.pause()
            card = app.query_one("ToolCard")
            assert card.status == "error"
            assert card.is_expanded
            assert result in card.output_text
            app.exit()
    asyncio.run(scenario())
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_tui.py::test_tool_error_card_stays_expanded_and_retains_full_result -q --basetemp testing/pytest-basetemp`

Expected: FAIL because current `ToolCard` clips content at 1200 characters and does not expose card state.

- [ ] **Step 3: Implement event adaptation and non-truncating cards**

```python
# tui_events.py
@dataclass(frozen=True)
class UiEvent:
    kind: str
    payload: dict[str, Any]
    task_id: str | None = None

    @classmethod
    def from_agent(cls, event: dict[str, Any]) -> "UiEvent":
        return cls(str(event.get("type") or "unknown"), dict(event), event.get("task_id"))

# ToolCard
@property
def is_expanded(self) -> bool:
    return bool(self._collapsible and not self._collapsible.collapsed)

@property
def output_text(self) -> str:
    return self._content

def set_success(self, content: str) -> None:
    self._content = content
    self._output_widget.update(content)
    self._collapsible.collapsed = True
```

Use a `VerticalScroll` inside the card body so full output remains accessible. Create one active streaming assistant and thinking widget per turn; update it per delta and finalize it at `turn_complete`/`done`.

- [ ] **Step 4: Run all TUI event tests**

Run: `python -m pytest tests/test_tui.py -q --basetemp testing/pytest-basetemp`

Expected: PASS; `text_delta` and `thinking_delta` are visible before `done`, success cards collapse, error cards expand.

- [ ] **Step 5: Commit live rendering**

```bash
git add src/paicli/render/tui_events.py src/paicli/render/textual_widgets.py src/paicli/render/tui_app.py tests/test_tui.py
git commit -m "feat: stream agent events in textual ui"
```

### Task 4: Move plan review and approval into Textual modal screens

**Files:**
- Create: `src/paicli/render/tui_dialogs.py`
- Modify: `src/paicli/render/tui_app.py`
- Modify: `src/paicli/entrypoints/repl.py`
- Modify: `tests/test_tui.py`

**Interfaces:**
- Produces `PlanReviewScreen(plan: ExecutionPlan)` with `dismiss(PlanReviewDecision)` and `ApprovalScreen(request: dict[str, Any])` with `dismiss("approve" | "reject")`.
- `PaiCliApp.review_plan(plan, can_replan: bool) -> Awaitable[PlanReviewDecision]` and `PaiCliApp.request_approval(request) -> Awaitable[str]`.
- `start_repl()` creates Agent with `approval_callback=tui_app.request_approval` after app construction, and routes `/plan` to `tui_app.run_plan_task`.

- [ ] **Step 1: Write failing modal decision tests**

```python
def test_plan_review_execute_returns_typed_decision():
    async def scenario():
        app = PaiCliApp(config=PaiCliConfig(), cwd=".")
        plan = ExecutionPlan(tasks=[PlanTask(id="one", description="read", type=TaskType.FILE_READ)])
        async with app.run_test() as pilot:
            decision_task = asyncio.create_task(app.review_plan(plan, can_replan=False))
            await pilot.pause()
            await pilot.press("enter")
            assert (await decision_task).action == "execute"
            app.exit()
    asyncio.run(scenario())
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_tui.py::test_plan_review_execute_returns_typed_decision -q --basetemp testing/pytest-basetemp`

Expected: FAIL because `PaiCliApp.review_plan` does not exist.

- [ ] **Step 3: Implement native modal screens and callback wiring**

```python
# tui_dialogs.py
class PlanReviewScreen(ModalScreen[PlanReviewDecision]):
    BINDINGS = [("enter", "execute", "Execute"), ("i", "supplement", "Supplement"), ("escape", "cancel", "Cancel")]
    def action_execute(self) -> None:
        self.dismiss(PlanReviewDecision.execute())

class ApprovalScreen(ModalScreen[str]):
    BINDINGS = [("y", "approve", "Approve"), ("n", "reject", "Reject"), ("escape", "reject", "Reject")]
    def action_approve(self) -> None:
        self.dismiss("approve")
```

Replace the old `RichRenderer`/`Prompt` review functions in the interactive path. Preserve `_run_plan_agent` domain execution semantics by moving its event sink and review callback into `PaiCliApp`; do not duplicate planner/executor logic.

- [ ] **Step 4: Run plan and approval tests**

Run: `python -m pytest tests/test_tui.py tests/test_plan.py -q --basetemp testing/pytest-basetemp`

Expected: PASS; pressing Enter returns execute, Escape returns cancel/reject, and existing executor behavior stays green.

- [ ] **Step 5: Commit native decisions**

```bash
git add src/paicli/render/tui_dialogs.py src/paicli/render/tui_app.py src/paicli/entrypoints/repl.py tests/test_tui.py
git commit -m "feat: add textual plan and approval dialogs"
```

### Task 5: Complete command handling, Windows smoke tests, and documentation

**Files:**
- Modify: `src/paicli/render/tui_app.py`
- Modify: `src/paicli/entrypoints/repl.py`
- Modify: `tests/test_tui.py`
- Modify: `README.md`

**Interfaces:**
- All entries in `SLASH_COMMANDS` are dispatched from `PaiCliApp` to shared service helpers.
- `action_interrupt()` cancels the worker when active; `action_quit()` exits only when idle.

- [ ] **Step 1: Write failing command, cancel, and narrow-layout tests**

```python
def test_interrupt_cancels_running_agent_without_exiting_app():
    async def scenario():
        app = PaiCliApp(agent=BlockingAgent(), config=PaiCliConfig(), cwd=".")
        async with app.run_test(size=(60, 20)) as pilot:
            app.run_agent_task("wait")
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app.is_running is False
            assert app.is_exiting is False
            app.exit()
    asyncio.run(scenario())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_tui.py -q --basetemp testing/pytest-basetemp`

Expected: FAIL because Ctrl+C is permanently bound to quit and narrow-screen behavior is not asserted.

- [ ] **Step 3: Implement final command and Windows behavior**

```python
def action_interrupt(self) -> None:
    if self._worker and self._worker.is_running:
        self._worker.cancel()
        self._running = False
        self._phase = "idle"
        self.query_one(Input).disabled = False
        self.query_one(Input).focus()
        return
    self.exit()
```

Bind `ctrl+c` to `interrupt`; keep `ctrl+q` as an explicit immediate quit. Replace per-command in-app reimplementations with shared async helpers from `repl.py` so `/memory`, `/mcp`, `/snapshot`, `/browser`, `/task`, `/policy`, `/audit`, `/skill`, `/index`, `/search`, `/restore`, `/model`, and `/hitl` retain their existing behavior. Update README with PowerShell 7 / Windows Terminal requirements and TUI shortcuts.

- [ ] **Step 4: Run full validation**

Run:

```bash
python -m pytest -q --basetemp testing/pytest-basetemp
python -m compileall -q src
python -m ruff check src tests
python -m paicli --help
```

Expected: all tests pass; compile and Ruff exit 0; help prints without a traceback.

- [ ] **Step 5: Run Windows PTY smoke verification**

Run: `python -m paicli`

Expected: Aurora screen renders, the caret is immediately in the input box, `/help` displays inside the chat log, and `Ctrl+C` exits from idle.

- [ ] **Step 6: Commit final integration**

```bash
git add src/paicli/render/tui_app.py src/paicli/entrypoints/repl.py tests/test_tui.py README.md
git commit -m "feat: complete windows textual cli interaction"
```

## Self-Review

- Spec coverage: Tasks 1–3 implement focus, Windows-first styling, streaming and cards; Task 4 implements native plan/approval; Task 5 restores command/cancel behavior, documents Windows requirements, and performs acceptance checks.
- Placeholder scan: every implementation step includes a concrete test, command, and expected result.
- Interface consistency: `PromptHistory`, `UiEvent`, `PlanReviewScreen`, `ApprovalScreen`, `review_plan`, and `request_approval` are introduced before their consumers.
