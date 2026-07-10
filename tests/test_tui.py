from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Static, TextArea

from paicli.render.history import PromptHistory
from paicli.render.textual_widgets import ChatLog, CommandInput
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
