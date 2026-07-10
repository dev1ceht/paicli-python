from __future__ import annotations

import asyncio
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Static, TextArea

from paicli.render.history import PromptHistory
from paicli.render.textual_widgets import ChatLog, CommandInput, StatusBar, ToolCard
from paicli.render.tui_app import PaiCliApp


def test_tui_focuses_text_area_and_streams_text_before_done():
    async def run() -> None:
        app = PaiCliApp(cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()

            focused = app.focused
            assert isinstance(focused, TextArea)
            assert focused is app.query_one(TextArea)

            chat_log = app.query_one(ChatLog)
            app.handle_event({"type": "text_delta", "text": "hello"})
            assert "hello" in chat_log.renderable_text()

            app.handle_event({"type": "done", "total_tokens": 0, "total_turns": 1})
            assert "hello" in chat_log.renderable_text()

    asyncio.run(run())


def test_tui_merges_incremental_text_deltas_into_one_visible_stream():
    async def run() -> None:
        app = PaiCliApp(cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            chat_log = app.query_one(ChatLog)

            app.handle_event({"type": "text_delta", "text": "hello"})
            app.handle_event({"type": "text_delta", "text": " world"})
            await pilot.pause()

            assert "hello world" in chat_log.renderable_text()

            app.handle_event({"type": "done", "total_tokens": 0, "total_turns": 1})
            assert "hello world" in chat_log.renderable_text()

    asyncio.run(run())


def test_tui_shows_thinking_delta_before_done():
    async def run() -> None:
        app = PaiCliApp(cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            chat_log = app.query_one(ChatLog)

            app.handle_event({"type": "thinking_delta", "thinking": "pondering"})
            await pilot.pause()

            assert "pondering" in chat_log.renderable_text()

    asyncio.run(run())


def test_tool_success_card_collapses_after_result():
    async def run() -> None:
        app = PaiCliApp(cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            app.handle_event(
                {"type": "tool_call", "name": "read_file", "input": {"path": "a.py"}}
            )
            app.handle_event(
                {"type": "tool_result", "name": "read_file", "result": "ok", "is_error": False}
            )
            await pilot.pause()

            card = app.query_one(ToolCard)
            assert card.status == "success"
            assert card.is_expanded is False
            assert card.output_text == "ok"

    asyncio.run(run())


def test_tool_error_card_stays_expanded_and_retains_full_result():
    async def run() -> None:
        app = PaiCliApp(cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            result = "x" * 5000
            app.handle_event(
                {"type": "tool_call", "name": "read_file", "input": {"path": "a.py"}}
            )
            app.handle_event(
                {
                    "type": "tool_result",
                    "name": "read_file",
                    "result": result,
                    "is_error": True,
                }
            )
            await pilot.pause()

            card = app.query_one(ToolCard)
            assert card.status == "error"
            assert card.is_expanded is True
            assert card.output_text == result

    asyncio.run(run())


def test_ui_event_from_agent_preserves_task_id():
    from paicli.render.tui_events import UiEvent

    event = UiEvent.from_agent(
        {
            "type": "task_tool_result",
            "task_id": "task-7",
            "name": "read_file",
            "result": "done",
        }
    )

    assert event.kind == "task_tool_result"
    assert event.task_id == "task-7"
    assert event.payload["name"] == "read_file"


def test_prompt_history_round_trips_utf8_messages(tmp_path):
    history = PromptHistory(tmp_path / "prompt_history.txt")
    message = "解释 Textual\n的输入行为"

    history.append(message)

    assert history.previous() == message
    assert history.next() == ""

    reloaded = PromptHistory(tmp_path / "prompt_history.txt")
    assert reloaded.previous() == message


def test_info_markup_is_rendered_not_shown_as_literal_tags():
    class LogApp(App[None]):
        def compose(self) -> ComposeResult:
            yield ChatLog()

    async def run() -> None:
        app = LogApp()
        async with app.run_test(size=(80, 24)) as pilot:
            chat_log = app.query_one(ChatLog)
            chat_log.add_info("[red]Error:[/red] failed")
            await pilot.pause()

            assert "Error: failed" in chat_log.renderable_text()
            assert "[red]" not in chat_log.renderable_text()

    asyncio.run(run())


class CommandInputHarness(App[None]):
    def __init__(self, *, history: PromptHistory | None = None) -> None:
        super().__init__()
        self.history = history
        self.submissions: list[str] = []
        self.submit_actions = 0

    def compose(self) -> ComposeResult:
        yield CommandInput(
            history=self.history,
            slash_commands=["/help", "/clear"],
            placeholder="Type your message or /command",
            compact=True,
        )

    def on_command_input_message_submitted(
        self, message: CommandInput.MessageSubmitted
    ) -> None:
        self.submissions.append(message.value)

    def action_submit_message(self) -> None:
        self.submit_actions += 1


def test_command_input_handles_enter_only_while_focused_and_keeps_shift_enter_newline():
    class PlanReviewProbe(Static):
        can_focus = True
        BINDINGS = [Binding("enter", "approve", "Approve", show=False)]

        def __init__(self) -> None:
            super().__init__()
            self.approved = False

        def action_approve(self) -> None:
            self.approved = True

    class RecordingApp(PaiCliApp):
        def __init__(self) -> None:
            super().__init__(cwd=".")
            self.submissions = 0

        def action_submit_message(self) -> None:
            self.submissions += 1

    async def run() -> None:
        app = RecordingApp()
        async with app.run_test(size=(80, 24)) as pilot:
            command_input = app.query_one(CommandInput)
            await pilot.press("enter")
            assert app.submissions == 1

            command_input.insert("draft")
            await pilot.press("shift+enter")
            assert command_input.text == "draft\n"
            assert app.submissions == 1

            probe = PlanReviewProbe()
            await app.mount(probe)
            probe.focus()
            await pilot.press("enter")
            assert probe.approved is True
            assert app.submissions == 1

    asyncio.run(run())


def test_command_input_posts_submission_message_on_enter():
    async def run() -> None:
        app = CommandInputHarness()
        async with app.run_test(size=(80, 24)) as pilot:
            command_input = app.query_one(CommandInput)
            command_input.focus()
            command_input.insert("draft")

            await pilot.press("enter")
            await pilot.pause()

            assert app.submissions == ["draft"]
            assert app.submit_actions == 1

    asyncio.run(run())


def test_command_input_uses_history_only_for_empty_or_single_line_input(tmp_path):
    history = PromptHistory(tmp_path / "prompt_history.txt")
    history.append("first command")
    history.append("第二条命令")

    async def run() -> None:
        app = CommandInputHarness(history=history)
        async with app.run_test(size=(80, 24)) as pilot:
            command_input = app.query_one(CommandInput)
            command_input.focus()

            await pilot.press("up")
            assert command_input.text == "第二条命令"

            await pilot.press("down")
            assert command_input.text == ""

            command_input.load_text("draft")
            await pilot.press("up")
            assert command_input.text == "第二条命令"

            command_input.load_text("line 1\nline 2")
            await pilot.press("up")
            assert command_input.text == "line 1\nline 2"

    asyncio.run(run())


def test_command_input_tab_completes_slash_commands():
    async def run() -> None:
        app = CommandInputHarness()
        async with app.run_test(size=(80, 24)) as pilot:
            command_input = app.query_one(CommandInput)
            command_input.focus()
            command_input.insert("/he")

            await pilot.press("tab")
            await pilot.pause()

            assert command_input.text == "/help"

    asyncio.run(run())


def test_tui_mounted_input_uses_persisted_prompt_history(tmp_path, monkeypatch):
    home_dir = tmp_path / "home"
    history_path = home_dir / ".paicli" / "history" / "prompt_history.txt"
    PromptHistory(history_path).append("saved prompt")

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home_dir))

    async def run() -> None:
        app = PaiCliApp(cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            command_input = app.query_one(CommandInput)
            command_input.focus()

            await pilot.press("up")
            assert command_input.text == "saved prompt"

            await pilot.press("down")
            assert command_input.text == ""

    asyncio.run(run())


def test_status_bar_render_uses_exact_phase_and_cost_colors():
    status_bar = StatusBar()
    status_bar.phase = "running"
    status_bar.model = "test-model"
    status_bar.context_text = "ctx 12%"
    status_bar.cost_text = "$0.1234"

    rendered = status_bar.render()

    assert "[bold #a8ff60]● running[/bold #a8ff60]" in rendered
    assert "[bold #facc15]$0.1234[/bold #facc15]" in rendered
