from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from rich.panel import Panel
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Static, TextArea

from paicli.config import PaiCliConfig
from paicli.render.history import PromptHistory
from paicli.render.textual_widgets import (
    ActivityRail,
    ChatLog,
    CommandInput,
    InputBar,
    MessageBlock,
    StartupBanner,
    StatusBar,
    ThinkingBlock,
    ToolCard,
    status_glyph,
)
from paicli.render.tui_app import PaiCliApp


def test_startup_banner_counts_builtin_tools_skills_and_enabled_mcp_servers(monkeypatch):
    class FakeRegistry:
        def list_names(self):
            return ["read_file", "write_file", "mcp__github__search", "mcp__browser__tabs"]

    class FakeMcpManager:
        specs = {
            "github": SimpleNamespace(enabled=True),
            "browser": SimpleNamespace(enabled=True),
            "disabled": SimpleNamespace(enabled=False),
        }

    monkeypatch.setattr(
        "paicli.skill.registry.SkillRegistry.list",
        lambda _self: [SimpleNamespace(name="code-review"), SimpleNamespace(name="research")],
    )

    app = PaiCliApp(registry=FakeRegistry(), mcp_manager=FakeMcpManager(), cwd=".")

    assert app._startup_capability_counts() == {"tools": 2, "skills": 2, "mcp_servers": 2}


def test_tui_renders_compact_full_width_startup_banner(monkeypatch):
    class FakeRegistry:
        def list_names(self):
            return ["read_file", "mcp__github__search"]

    class FakeMcpManager:
        specs = {"github": SimpleNamespace(enabled=True)}

    config = SimpleNamespace(
        llm=SimpleNamespace(model="test-model", provider="test-provider"),
        policy=SimpleNamespace(hitl_mode="auto"),
    )
    monkeypatch.setattr("paicli.skill.registry.SkillRegistry.list", lambda _self: [object()])

    async def run() -> None:
        app = PaiCliApp(
            config=config,
            cwd="D:/project/PaiCLI-Python",
            registry=FakeRegistry(),
            mcp_manager=FakeMcpManager(),
        )
        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause()
            banner = app.query_one(StartupBanner)

            assert "PAICLI  v0.1.0" in banner.plain_text
            assert "Ready to build" in banner.plain_text
            assert "Tools: 1" in banner.plain_text
            assert "Skills: 1" in banner.plain_text
            assert "MCP: 1 servers" in banner.plain_text
            assert "D:/project/PaiCLI-Python" in banner.plain_text
            assert "/help commands" in banner.plain_text
            assert len(banner.plain_text.splitlines()) <= 5

    asyncio.run(run())


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


def test_tui_updates_status_bar_from_plan_usage_events():
    async def run() -> None:
        app = PaiCliApp(cwd=".")
        app._context_window = 1_000

        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()

            app.handle_event({"type": "plan_generation_started", "goal": "inspect"})
            app.handle_event(
                {
                    "type": "context_status",
                    "pressure_tier": "tier1_snip",
                    "pressure_ratio": 0.60,
                    "estimated": True,
                }
            )
            app.handle_event(
                {
                    "type": "usage",
                    "usage": {"input_tokens": 11, "output_tokens": 7, "cached_tokens": 5},
                }
            )
            app.handle_event(
                {
                    "type": "usage",
                    "usage": {"input_tokens": 13, "output_tokens": 17, "cached_tokens": 3},
                }
            )

            status_bar = app.query_one(StatusBar)
            assert status_bar.phase == "plan"
            assert status_bar.context_text == "ctx 13/1.0k (1%)"
            assert status_bar.pressure_text == "pressure ~60%"
            assert status_bar.token_detail == "in:13 out:17 cached:3"

            app.handle_event({"type": "plan_completed", "results": {}})
            assert app._last_total_tokens == 48
            assert app._phase == "idle"

    asyncio.run(run())


def test_tui_context_bar_shows_max_active_request_then_retained_baseline():
    async def run() -> None:
        app = PaiCliApp(cwd=".")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()

            app.handle_event(
                {
                    "type": "context_usage",
                    "state": "retained",
                    "estimated": True,
                    "used_tokens": 80,
                    "input_tokens": 80,
                    "output_tokens": 0,
                    "cached_tokens": 0,
                    "context_window": 1_000,
                }
            )
            app.handle_event(
                {
                    "type": "context_usage",
                    "state": "active",
                    "request_id": "request-a",
                    "scope": "agent",
                    "estimated": True,
                    "used_tokens": 100,
                    "input_tokens": 90,
                    "output_tokens": 10,
                    "cached_tokens": 0,
                    "context_window": 1_000,
                }
            )
            app.handle_event(
                {
                    "type": "context_usage",
                    "state": "active",
                    "request_id": "request-b",
                    "scope": "task:test",
                    "estimated": True,
                    "used_tokens": 200,
                    "input_tokens": 150,
                    "output_tokens": 50,
                    "cached_tokens": 20,
                    "context_window": 1_000,
                }
            )

            status_bar = app.query_one(StatusBar)
            assert status_bar.context_text == "ctx max ~200/1.0k (20%) · 2 active"

            app.handle_event({"type": "context_request_finished", "request_id": "request-b"})
            assert status_bar.context_text == "ctx ~100/1.0k (10%)"

            app.handle_event({"type": "context_request_finished", "request_id": "request-a"})
            assert status_bar.context_text == "ctx ~80/1.0k (8%)"

    asyncio.run(run())


def test_tui_starts_with_the_agent_base_context_estimate():
    class ContextAgent:
        def context_usage_event(self):
            return {
                "type": "context_usage",
                "state": "retained",
                "estimated": True,
                "used_tokens": 42,
                "input_tokens": 42,
                "output_tokens": 0,
                "cached_tokens": 0,
                "context_window": 1_000,
            }

    async def run() -> None:
        app = PaiCliApp(agent=ContextAgent(), cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()

            status = app.query_one(StatusBar)
            assert status.context_text == "ctx ~42/1.0k (4%)"
            assert status.token_detail == ""

    asyncio.run(run())


def test_command_dock_has_no_persistent_shortcut_footer():
    async def run() -> None:
        app = PaiCliApp(cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()

            assert len(app.query(Footer)) == 0
            assert app.query_one(StatusBar).styles.height.value == 1
            children = list(app.screen.children)
            assert children.index(app.query_one(InputBar)) < children.index(
                app.query_one(StatusBar)
            )

    asyncio.run(run())


def test_command_input_grows_with_multiline_draft_and_resets_after_submit():
    async def run() -> None:
        app = PaiCliApp(cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            command_input = app.query_one(CommandInput)
            input_bar = app.query_one(InputBar)

            assert command_input.size.height == 2
            assert len(input_bar.query("Label")) == 0

            await pilot.press("shift+enter", "shift+enter", "shift+enter")
            await pilot.pause()

            assert command_input.size.height == 4
            assert input_bar.size.height == 4

            command_input.load_text("/help")
            await pilot.press("enter")
            await pilot.pause()

            assert command_input.size.height == 2

    asyncio.run(run())


def test_startup_banner_keeps_its_adaptive_height_after_first_submission():
    async def run() -> None:
        app = PaiCliApp(cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            banner = app.query_one(StartupBanner)
            original_height = banner.size.height
            original_text = banner.plain_text

            app._submit_message("/help")
            await pilot.pause()

            assert app.query_one(StartupBanner) is banner
            assert banner.size.height == original_height
            assert banner.plain_text == original_text

    asyncio.run(run())


def test_tui_distinguishes_live_estimates_from_last_actual_usage():
    async def run() -> None:
        app = PaiCliApp(cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()

            app.handle_event(
                {
                    "type": "context_usage",
                    "state": "retained",
                    "scope": "agent",
                    "estimated": True,
                    "used_tokens": 80,
                    "input_tokens": 80,
                    "output_tokens": 0,
                    "cached_tokens": 0,
                    "context_window": 1_000,
                }
            )
            app.handle_event(
                {
                    "type": "context_usage",
                    "state": "active",
                    "request_id": "request-a",
                    "scope": "agent",
                    "estimated": True,
                    "used_tokens": 100,
                    "input_tokens": 90,
                    "output_tokens": 10,
                    "cached_tokens": 0,
                    "context_window": 1_000,
                }
            )
            status = app.query_one(StatusBar)
            assert status.token_detail == "in:~90 out:~10"

            app.handle_event(
                {
                    "type": "usage",
                    "usage": {"input_tokens": 120, "output_tokens": 5, "cached_tokens": 40},
                }
            )
            app.handle_event(
                {
                    "type": "context_usage",
                    "state": "active",
                    "request_id": "request-a",
                    "scope": "agent",
                    "estimated": False,
                    "used_tokens": 125,
                    "input_tokens": 120,
                    "output_tokens": 5,
                    "cached_tokens": 40,
                    "context_window": 1_000,
                }
            )
            assert status.token_detail == "in:120 out:5 cached:40"

            app.handle_event({"type": "context_request_finished", "request_id": "request-a"})
            assert status.context_text == "ctx ~80/1.0k (8%)"
            assert status.token_detail == "last in:120 out:5 cached:40"

    asyncio.run(run())


def test_tui_enter_submits_message_and_sets_running_state():
    class WaitingAgent:
        async def run(self, message: str):
            assert message == "hello"
            await asyncio.Event().wait()
            yield {"type": "done", "total_tokens": 0, "total_turns": 1}

    async def run() -> None:
        app = PaiCliApp(agent=WaitingAgent(), cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            command_input = app.query_one(CommandInput)
            command_input.insert("hello")

            await pilot.press("enter")
            await pilot.pause()

            assert app._agent_running is True
            log_text = app.query_one(ChatLog).renderable_text()
            assert app._phase == "running", log_text
            assert "hello" in log_text

            app.handle_event(
                {
                    "type": "context_usage",
                    "state": "active",
                    "request_id": "cancel-me",
                    "scope": "agent",
                    "estimated": True,
                    "used_tokens": 100,
                    "input_tokens": 100,
                    "output_tokens": 0,
                    "cached_tokens": 0,
                    "context_window": 1_000,
                }
            )

            app.action_interrupt()
            await pilot.pause()
            assert app._agent_running is False
            assert app._context_usage.active_count == 0

    asyncio.run(run())


def test_tui_help_renders_literal_bracketed_arguments():
    async def run() -> None:
        app = PaiCliApp(cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            command_input = app.query_one(CommandInput)
            command_input.insert("/help")

            await pilot.press("enter")
            await pilot.pause()

            rendered = app.query_one(ChatLog).renderable_text()
            assert "/index [path]" in rendered
            assert "Ctrl+End" in rendered

    asyncio.run(run())


def test_tui_clear_preserves_startup_banner():
    async def run() -> None:
        app = PaiCliApp(cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            chat_log = app.query_one(ChatLog)
            banner = app.query_one(StartupBanner)
            chat_log.add_user_message("temporary conversation")
            app._handle_slash_command("/clear")
            await pilot.pause()

            text = chat_log.renderable_text()
            assert "Ready to build" in text
            assert "temporary conversation" not in text
            assert app.query_one(StartupBanner) is banner

    asyncio.run(run())


def test_tui_clear_only_clears_display_and_reset_clears_session_history():
    class SessionAgent:
        def __init__(self):
            self.history = ["retained"]
            self.reset_calls = 0

        def clear_history(self):
            self.reset_calls += 1
            self.history.clear()

        def context_usage_event(self):
            return {
                "type": "context_usage",
                "state": "retained",
                "estimated": True,
                "used_tokens": 10 if self.history else 2,
                "input_tokens": 10 if self.history else 2,
                "output_tokens": 0,
                "cached_tokens": 0,
                "context_window": 100,
            }

    async def run() -> None:
        agent = SessionAgent()
        app = PaiCliApp(agent=agent, cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()

            app._handle_slash_command("/clear")
            assert agent.history == ["retained"]
            assert agent.reset_calls == 0

            app._handle_slash_command("/reset")
            assert agent.history == []
            assert agent.reset_calls == 1
            assert app.query_one(StatusBar).context_text == "ctx ~2/100 (2%)"

    asyncio.run(run())


def test_tui_context_command_shows_retained_active_calibration_and_unknown_limit(tmp_path):
    class ContextManagerStatus:
        def get_status(self):
            return {
                "last_compaction": {"compacted_items": 4, "used_llm": True},
            }

    class ContextAgent:
        llm_client = SimpleNamespace(
            model_name="unknown-model",
            provider_name="test",
            max_context_window=128_000,
            reported_context_window=None,
            context_estimator=SimpleNamespace(
                get_calibration_factor=lambda: 1.25,
                sample_count=3,
            ),
        )
        context_manager = ContextManagerStatus()

        def context_usage_event(self):
            return {
                "type": "context_usage",
                "state": "retained",
                "estimated": True,
                "used_tokens": 50,
                "input_tokens": 50,
                "output_tokens": 0,
                "cached_tokens": 0,
                "context_window": None,
                "safety_context_window": 128_000,
            }

    config = PaiCliConfig()
    config.memory.long_term_path = str(tmp_path / "memory.json")
    config.llm.model = "unknown-model"
    config.llm.provider = "test"
    registry = SimpleNamespace(list_names=lambda: [])

    async def run() -> None:
        app = PaiCliApp(
            agent=ContextAgent(),
            config=config,
            registry=registry,
            cwd=str(tmp_path),
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.handle_event(
                {
                    "type": "context_usage",
                    "state": "active",
                    "request_id": "request-a",
                    "scope": "planner",
                    "estimated": True,
                    "used_tokens": 80,
                    "input_tokens": 80,
                    "output_tokens": 0,
                    "cached_tokens": 0,
                    "context_window": None,
                    "safety_context_window": 128_000,
                }
            )
            app._handle_slash_command("/context")
            await pilot.pause()

            text = app.query_one(ChatLog).renderable_text()
            assert "model limit: unknown" in text
            assert "safety budget: 128.0k" in text
            assert "retained: ~50/?" in text
            assert "planner: ~80/?" in text
            assert "calibration: 1.25 (3 samples)" in text
            assert "compaction: 4 items (llm)" in text

    asyncio.run(run())


def test_ctrl_y_toggles_hitl_between_auto_and_unattended():
    config = SimpleNamespace(
        llm=SimpleNamespace(model="test-model", provider="test-provider"),
        policy=SimpleNamespace(hitl_mode="auto"),
    )

    async def run() -> None:
        app = PaiCliApp(config=config, cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            chat_log = app.query_one(ChatLog)
            banner = app.query_one(StartupBanner)
            await pilot.press("ctrl+y")
            await pilot.pause()
            assert config.policy.hitl_mode == "never"
            assert "HITL switched to unattended mode" in chat_log.renderable_text()
            assert app.query_one(StartupBanner) is banner
            assert list(chat_log.children)[0] is banner

            await pilot.press("ctrl+y")
            await pilot.pause()
            assert config.policy.hitl_mode == "auto"
            assert "HITL switched to auto mode" in chat_log.renderable_text()

    asyncio.run(run())


def test_hitl_command_refreshes_startup_banner():
    config = SimpleNamespace(
        llm=SimpleNamespace(model="test-model", provider="test-provider"),
        policy=SimpleNamespace(hitl_mode="auto"),
    )

    async def run() -> None:
        app = PaiCliApp(config=config, cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            banner = app.query_one(StartupBanner)
            app._handle_slash_command("/hitl always")
            await pilot.pause()

            assert config.policy.hitl_mode == "always"
            assert app.query_one(StartupBanner) is banner
            assert "HITL ALWAYS" in banner.plain_text

    asyncio.run(run())


def test_tui_model_command_rebuilds_the_idle_agent_and_refreshes_ui(tmp_path):
    class SwitchingAgent:
        def __init__(self, config):
            self.config = config
            self.history = ["preserved"]

        def reconfigure_llm(self, llm_config):
            self.config.llm = llm_config
            return SimpleNamespace(
                model_name=llm_config.model,
                provider_name=llm_config.provider,
                max_context_window=128_000,
            )

        def context_usage_event(self):
            return {
                "type": "context_usage",
                "state": "retained",
                "estimated": True,
                "used_tokens": 7,
                "input_tokens": 7,
                "output_tokens": 0,
                "cached_tokens": 0,
                "context_window": 128_000,
            }

    (tmp_path / ".env").write_text(
        "PAICLI_QWEN_API_KEY=qwen-key\nPAICLI_QWEN_BASE_URL=https://qwen.example/v1\n",
        encoding="utf-8",
    )
    config = PaiCliConfig()
    agent = SwitchingAgent(config)

    async def run() -> None:
        app = PaiCliApp(agent=agent, config=config, cwd=str(tmp_path))
        app._model = config.llm.model
        app._provider = config.llm.provider
        async with app.run_test(size=(80, 24)) as pilot:
            app.handle_event(
                {
                    "type": "context_usage",
                    "state": "active",
                    "request_id": "old-model-request",
                    "scope": "agent",
                    "estimated": True,
                    "used_tokens": 99,
                    "input_tokens": 99,
                    "output_tokens": 0,
                    "cached_tokens": 0,
                    "context_window": 128_000,
                }
            )
            app._model_command("qwen qwen-turbo", app.query_one(ChatLog))
            await pilot.pause()

            assert config.llm.provider == "qwen"
            assert config.llm.model == "qwen-turbo"
            assert agent.history == ["preserved"]
            assert app._context_window == 128_000
            assert app._context_usage.active_count == 0
            assert app.query_one(StatusBar).context_text == "ctx ~7/128.0k (0%)"
            assert "qwen-turbo" in app.query_one(StartupBanner).plain_text
            assert (
                "Model switched to qwen-turbo (qwen)." in app.query_one(ChatLog).renderable_text()
            )

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
            app.handle_event({"type": "tool_call", "name": "read_file", "input": {"path": "a.py"}})
            app.handle_event(
                {"type": "tool_result", "name": "read_file", "result": "ok", "is_error": False}
            )
            await pilot.pause()

            card = app.query_one(ToolCard)
            assert card.status == "success"
            assert card.is_expanded is False
            assert card.output_text == "ok"
            assert "Success" in card.plain_text

    asyncio.run(run())


def test_tool_error_card_stays_expanded_and_retains_full_result():
    async def run() -> None:
        app = PaiCliApp(cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            result = "Permission denied: a.py\n" + "x" * 5000
            app.handle_event({"type": "tool_call", "name": "read_file", "input": {"path": "a.py"}})
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
            assert card.error_summary == "Permission denied: a.py"
            assert card.has_class("tool-error")

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


def test_prompt_history_append_survives_write_failure(tmp_path):
    """append() must keep the item in memory even when the file cannot be written."""
    from unittest.mock import patch

    history_file = tmp_path / "prompt_history.txt"
    history = PromptHistory(history_file)

    with patch.object(Path, "write_text", side_effect=PermissionError("denied")):
        # Must not raise despite the write failing
        history.append("hello")

    # Item is still navigable in memory
    assert history.previous() == "hello"


def test_command_input_submits_even_when_history_write_fails(tmp_path):
    """Enter must still reach the app when PromptHistory cannot persist."""
    from unittest.mock import patch

    history_file = tmp_path / "prompt_history.txt"
    history = PromptHistory(history_file)

    async def run() -> None:
        app = CommandInputHarness(history=history)
        async with app.run_test(size=(80, 24)) as pilot:
            inp = app.query_one(CommandInput)
            inp.focus()
            await pilot.pause()
            with patch.object(Path, "write_text", side_effect=PermissionError("denied")):
                inp.load_text("test message")
                await pilot.press("enter")
                await pilot.pause()
            assert app.submit_actions == 1
            assert app.submissions == ["test message"]

    asyncio.run(run())


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

    def on_command_input_message_submitted(self, message: CommandInput.MessageSubmitted) -> None:
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


def test_conversation_follow_mode_preserves_history_position_until_resumed():
    async def run() -> None:
        app = PaiCliApp(cwd=".")
        async with app.run_test(size=(80, 12)) as pilot:
            chat_log = app.query_one(ChatLog)
            for index in range(20):
                chat_log.add_info(f"history {index}")
            await pilot.pause()
            chat_log.scroll_end(animate=False)
            await pilot.pause()

            chat_log.scroll_to(y=0, animate=False)
            await pilot.pause()
            assert chat_log.is_vertical_scroll_end is False

            chat_log.add_info("new activity")
            await pilot.pause()

            assert chat_log.scroll_y == 0
            assert chat_log.new_activity_pending is True

            chat_log.scroll_end(animate=False, immediate=True)
            await pilot.pause()

            assert chat_log.is_vertical_scroll_end is True
            assert chat_log.new_activity_pending is False

    asyncio.run(run())


def test_tool_retries_are_grouped_into_the_running_activity():
    async def run() -> None:
        app = PaiCliApp(cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            app.handle_event({"type": "tool_call", "name": "read_file", "input": {"path": "a.py"}})
            app.handle_event(
                {
                    "type": "retry",
                    "scope": "tool",
                    "tool_name": "read_file",
                    "attempt": 1,
                    "max_retries": 3,
                    "delay": 0.25,
                    "error_kind": "timeout",
                }
            )
            await pilot.pause()

            card = app.query_one(ToolCard)
            assert card.retry_count == 1
            assert "retry 1/3" in card.plain_text
            assert len(app.query(ActivityRail)) == 1

    asyncio.run(run())


def test_thinking_and_consecutive_tool_work_share_one_activity_rail():
    async def run() -> None:
        app = PaiCliApp(cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            app.handle_event({"type": "thinking_delta", "thinking": "inspect structure"})
            app.handle_event({"type": "tool_call", "name": "read_file", "input": {"path": "a.py"}})
            app.handle_event(
                {"type": "tool_result", "name": "read_file", "result": "ok", "is_error": False}
            )
            await pilot.pause()

            rails = list(app.query(ActivityRail))
            thinking = app.query_one(ThinkingBlock)
            tool = app.query_one(ToolCard)

            assert len(rails) == 1
            assert rails[0].item_count == 2
            assert thinking.is_expanded is False
            assert tool.is_expanded is False

    asyncio.run(run())


def test_startup_banner_uses_three_rows_at_the_80_column_baseline():
    async def run() -> None:
        app = PaiCliApp(cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            banner = app.query_one(StartupBanner)

            assert banner.is_compact is True
            assert banner.size.height == 3
            assert len(banner.plain_text.splitlines()) == 3

    asyncio.run(run())


def test_running_activity_pulse_stops_when_the_activity_completes():
    async def run() -> None:
        app = PaiCliApp(cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            app.handle_event({"type": "thinking_delta", "thinking": "inspect"})
            await pilot.pause()
            thinking = app.query_one(ThinkingBlock)
            assert thinking.animation_active is True

            app.handle_event({"type": "tool_call", "name": "read_file", "input": {"path": "a.py"}})
            await pilot.pause()
            tool = app.query_one(ToolCard)

            assert thinking.animation_active is False
            assert tool.animation_active is True

            app.handle_event(
                {"type": "tool_result", "name": "read_file", "result": "ok", "is_error": False}
            )
            await pilot.pause()

            assert tool.animation_active is False

    asyncio.run(run())


def test_conversation_canvas_uses_unboxed_assistant_output_and_compact_user_prompt():
    async def run() -> None:
        app = PaiCliApp(cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            chat_log = app.query_one(ChatLog)
            chat_log.add_assistant_text("answer")
            chat_log.add_user_message("question")
            await pilot.pause()

            assistant, user = list(app.query(MessageBlock))

            assert not isinstance(assistant.render(), Panel)
            assert not isinstance(user.render(), Panel)
            assert assistant.has_class("message-assistant")
            assert user.has_class("message-user")

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

    assert "[bold #60d8ff]● running[/bold #60d8ff]" in rendered
    assert "[bold #facc15]$0.1234[/bold #facc15]" in rendered

    status_bar.phase = "plan"
    assert "[bold #c084fc]◆ plan[/bold #c084fc]" in status_bar.render()


def test_status_glyph_uses_single_width_unicode_with_ascii_fallback():
    assert status_glyph("running", encoding="utf-8") == "●"
    assert status_glyph("success", encoding="utf-8") == "✓"
    assert status_glyph("running", encoding="ascii") == "*"
    assert status_glyph("success", encoding="ascii") == "OK"
    assert status_glyph("error", encoding="ascii") == "ERR"


def test_tui_context_pressure_colors_follow_confirmed_thresholds():
    async def run() -> None:
        app = PaiCliApp(cwd=".")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            status = app.query_one(StatusBar)

            for used, expected in [
                (590, "normal"),
                (600, "yellow"),
                (800, "orange"),
                (950, "red"),
            ]:
                app.handle_event(
                    {
                        "type": "context_usage",
                        "state": "retained",
                        "estimated": True,
                        "used_tokens": used,
                        "input_tokens": used,
                        "output_tokens": 0,
                        "cached_tokens": 0,
                        "context_window": 1_000,
                    }
                )
                assert status.context_level == expected

            app.handle_event(
                {
                    "type": "context_usage",
                    "state": "retained",
                    "estimated": True,
                    "used_tokens": 999,
                    "input_tokens": 999,
                    "output_tokens": 0,
                    "cached_tokens": 0,
                    "context_window": None,
                }
            )
            assert status.context_level == "neutral"

    asyncio.run(run())


def test_plan_review_screen_execute_returns_decision():
    from paicli.plan import ExecutionPlan, PlanTask, TaskType
    from paicli.render.tui_dialogs import PlanReviewScreen

    async def run() -> None:
        plan = ExecutionPlan(
            tasks=[
                PlanTask(
                    id="one",
                    description="read file",
                    type=TaskType.FILE_READ,
                )
            ]
        )

        result = [None]

        class ReviewApp(App[None]):
            def compose(self) -> ComposeResult:
                from textual.widgets import Footer, Label

                yield Label("Test App")
                yield Footer()

        app = ReviewApp()
        async with app.run_test(size=(80, 24)) as pilot:
            # Push the screen directly in a worker
            async def push_and_test():
                screen = PlanReviewScreen(plan)
                result[0] = await app.push_screen_wait(screen)

            app.run_worker(push_and_test)

            # Wait for screen to be pushed and mounted
            await pilot.pause()
            await pilot.pause()
            await pilot.pause()

            # Directly call the action (key bindings are tested separately)
            if isinstance(app.screen, PlanReviewScreen):
                app.screen.action_execute()
                await pilot.pause()
                await pilot.pause()

        assert result[0] is not None, "Result is None"
        assert result[0].action == "execute"

    asyncio.run(run())


def test_approval_screen_approve_returns_approve():
    from paicli.render.tui_dialogs import ApprovalScreen

    async def run() -> None:
        request = {
            "tool_name": "read_file",
            "danger_level": "safe",
            "input": "test.txt",
        }

        result: str | None = None

        class ApprovalApp(App[None]):
            def compose(self) -> ComposeResult:
                from textual.widgets import Footer

                yield Footer()

            def on_mount(self) -> None:
                async def _push():
                    nonlocal result
                    screen = ApprovalScreen(request)
                    result = await self.push_screen_wait(screen)

                self.run_worker(_push)

        app = ApprovalApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()

        assert result == "approve"

    asyncio.run(run())


def test_app_reviews_plan_inline_without_changing_screen():
    from paicli.plan import ExecutionPlan, PlanTask, TaskType
    from paicli.render.tui_dialogs import InlinePlanReview, PlanReviewScreen

    async def run() -> None:
        plan = ExecutionPlan(
            tasks=[PlanTask(id="one", description="read file", type=TaskType.FILE_READ)]
        )
        app = PaiCliApp(cwd=".")
        result = None

        async with app.run_test(size=(80, 24)) as pilot:

            async def review() -> None:
                nonlocal result
                result = await app.review_plan(plan)

            app.run_worker(review())
            await pilot.pause()
            await pilot.pause()

            review_widget = app.query_one(InlinePlanReview)
            assert not isinstance(app.screen, PlanReviewScreen)
            assert app.query_one(CommandInput).disabled is True
            assert "read file" in review_widget.plain_text

            await pilot.press("enter")
            await pilot.pause()

            assert result is not None
            assert result.action == "execute"
            assert review_widget.is_resolved is True
            assert app.query_one(CommandInput).disabled is False

    asyncio.run(run())


def test_app_requests_approval_inline_without_changing_screen():
    from paicli.render.tui_dialogs import ApprovalScreen, InlineApprovalRequest

    async def run() -> None:
        app = PaiCliApp(cwd=".")
        result: str | None = None

        async with app.run_test(size=(80, 24)) as pilot:
            chat_log = app.query_one(ChatLog)
            for index in range(20):
                chat_log.add_info(f"history {index}")
            await pilot.pause()
            chat_log.scroll_end(animate=False, immediate=True)
            chat_log.scroll_to(y=0, animate=False)
            await pilot.pause()

            async def request() -> None:
                nonlocal result
                result = await app.request_approval(
                    {
                        "tool_name": "write_file",
                        "danger_level": "write",
                        "description": "Write a file",
                        "input": {"path": "a.py", "content": "hello"},
                    }
                )

            app.run_worker(request())
            await pilot.pause()
            await pilot.pause()

            approval = app.query_one(InlineApprovalRequest)
            assert not isinstance(app.screen, ApprovalScreen)
            assert app.query_one(CommandInput).disabled is True
            assert chat_log.scroll_y == 0
            assert chat_log.new_activity_pending is True

            await pilot.press("y")
            await pilot.pause()

            assert result == "approve"
            assert approval.is_resolved is True
            assert app.query_one(CommandInput).disabled is False

    asyncio.run(run())


def test_approval_screen_deny_returns_deny():
    from paicli.render.tui_dialogs import ApprovalScreen

    async def run() -> None:
        request = {
            "tool_name": "read_file",
            "danger_level": "safe",
            "input": "test.txt",
        }

        result: str | None = None

        class DenyApp(App[None]):
            def compose(self) -> ComposeResult:
                from textual.widgets import Footer

                yield Footer()

            def on_mount(self) -> None:
                async def _push():
                    nonlocal result
                    screen = ApprovalScreen(request)
                    result = await self.push_screen_wait(screen)

                self.run_worker(_push)

        app = DenyApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()

        assert result == "deny"

    asyncio.run(run())


def test_interrupt_exits_when_idle():
    """Ctrl+C exits the app when not running."""

    async def run() -> None:
        app = PaiCliApp(cwd=".")
        exited = False
        original_exit = app.exit

        def mock_exit():
            nonlocal exited
            exited = True
            original_exit()

        app.exit = mock_exit

        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            # App is idle, Ctrl+C should exit
            app.action_interrupt()
            await pilot.pause()

        assert exited is True

    asyncio.run(run())


def test_interrupt_cancels_worker_when_running():
    """Ctrl+C cancels the worker when running."""

    async def run() -> None:
        app = PaiCliApp(cwd=".")

        async def long_task():
            await asyncio.sleep(10)  # Long-running task

        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            # Start a worker
            app._agent_running = True
            app._phase = "running"
            worker = app.run_worker(long_task())
            app._worker = worker
            await pilot.pause()

            # App is running, Ctrl+C should cancel
            app.action_interrupt()
            await pilot.pause()

            # Worker should be cancelled
            assert app._agent_running is False
            assert app._phase == "idle"
            assert app._worker is None

    asyncio.run(run())
