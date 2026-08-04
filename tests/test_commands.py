from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import TextArea

from paicli.commands import (
    ClearScreen,
    CommandContext,
    CommandExecutor,
    CommandRegistry,
    CommandResult,
    CommandSpec,
    CompletionItem,
    CompletionRequest,
    ExitApp,
    default_registry,
    legacy_argument_text,
    render_help,
)
from paicli.render.textual_widgets import CommandInput
from paicli.render.tui_app import PaiCliApp


class RecordingHost:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def dispatch_registered_command(self, invocation):
        self.calls.append((invocation.path, dict(invocation.values)))
        return CommandResult.empty()


def test_builtin_registry_parses_nested_commands_and_flags() -> None:
    registry = default_registry()

    parsed = registry.parse("/session share session-123 --include-tool-results")

    assert parsed.path == ("session", "share")
    assert parsed.values == {
        "session-id": "session-123",
        "include_tool_results": True,
    }


def test_builtin_registry_preserves_rest_arguments_after_flags() -> None:
    parsed = default_registry().parse("/save --global 记住这条事实")

    assert parsed.values["global"] is True
    assert parsed.values["事实"] == "记住这条事实"

    reordered = default_registry().parse("/save 另一条事实 --global")
    assert legacy_argument_text(reordered) == "--global 另一条事实"


def test_registry_preserves_windows_paths_in_arguments() -> None:
    parsed = default_registry().parse(r'/index "C:\Program Files\PaiCLI"')

    assert parsed.values["path"] == r"C:\Program Files\PaiCLI"


def test_help_is_derived_from_the_registry() -> None:
    text = render_help(default_registry())

    assert "/model - 查看当前模型" in text
    assert "/plan <任务内容> - 直接用计划模式执行这条任务" in text
    assert "/team" not in text
    assert "/session resume <session-id> - 恢复指定会话" in text
    assert "/browser connect <port> - 旧式 CDP 端口连接" in text


def test_registry_completion_handles_root_and_nested_commands() -> None:
    registry = default_registry()
    context = CommandContext(cwd=Path.cwd())

    root_items = registry.complete_sync(CompletionRequest("/mo", 3), context)
    child_items = registry.complete_sync(CompletionRequest("/session ", 9), context)
    flag_items = registry.complete_sync(
        CompletionRequest("/session share --include", len("/session share --include")),
        context,
    )

    assert [item.label for item in root_items] == ["/model"]
    assert "resume" in [item.label for item in child_items]
    assert [item.label for item in flag_items] == ["--include-tool-results"]


def test_registry_completion_uses_async_spec_provider() -> None:
    async def provider(_request, _context):
        return [CompletionItem("remote-session", kind="argument")]

    registry = CommandRegistry(
        (
            CommandSpec(
                name="lookup",
                description="lookup",
                handler=lambda *_: None,
                completion=provider,
            ),
        )
    )

    items = asyncio.run(
        registry.complete(
            CompletionRequest("/lookup ", len("/lookup ")),
            CommandContext(cwd=Path.cwd()),
        )
    )

    assert [item.label for item in items] == ["remote-session"]


def test_executor_uses_the_registered_handler_and_returns_parse_errors() -> None:
    host = RecordingHost()
    executor = CommandExecutor(default_registry())

    async def run() -> None:
        result = await executor.execute(
            "/session resume session-123",
            CommandContext(cwd=Path.cwd(), host=host),
        )
        assert not result.messages

        error = await executor.execute(
            "/session restore",
            CommandContext(cwd=Path.cwd(), host=host),
        )
        assert error.messages
        assert "缺少参数" in error.messages[0].text

    asyncio.run(run())
    assert host.calls == [(('session', 'resume'), {"session-id": "session-123"})]


def test_core_handlers_return_ui_neutral_results_without_a_host() -> None:
    executor = CommandExecutor(default_registry())
    context = CommandContext(cwd=Path.cwd(), registry=default_registry())

    async def run() -> None:
        help_result = await executor.execute("/help", context)
        assert help_result.messages
        assert "/model - 查看当前模型" in help_result.messages[0].text

        clear_result = await executor.execute("/clear", context)
        assert isinstance(clear_result.effects[0], ClearScreen)

        exit_result = await executor.execute("/exit", context)
        assert isinstance(exit_result.effects[0], ExitApp)

    asyncio.run(run())


def test_registry_rejects_duplicate_aliases() -> None:
    with pytest.raises(ValueError, match="duplicate command"):
        CommandRegistry(
            (
                CommandSpec(name="one", description="one", handler=lambda *_: None, aliases=("x",)),
                CommandSpec(name="two", description="two", handler=lambda *_: None, aliases=("x",)),
            )
        )


def test_mounted_tui_input_uses_the_shared_registry_for_completion() -> None:
    async def run() -> None:
        app = PaiCliApp(cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            command_input = app.query_one(CommandInput)
            assert isinstance(command_input, TextArea)
            command_input.focus()
            command_input.insert("/mo")

            await pilot.press("tab")
            await pilot.pause()

            assert command_input.text == "/model"

            command_input._set_text_value("/session resum")
            await pilot.press("tab")
            await pilot.pause()

            assert command_input.text == "/session resume"

    asyncio.run(run())
