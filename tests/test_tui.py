from __future__ import annotations

import asyncio

from textual.binding import Binding
from textual.widgets import Static, TextArea

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
