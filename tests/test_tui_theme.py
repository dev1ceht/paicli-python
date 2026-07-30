import asyncio

from paicli.render.textual_widgets import StatusBar
from paicli.render.tui_app import PaiCliApp
from paicli.render.tui_theme import PI_DARK


def test_pi_dark_theme_exposes_semantic_conversation_colors():
    assert PI_DARK.accent == "#8abeb7"
    assert PI_DARK.user_message_bg == "#343541"
    assert PI_DARK.tool_pending_bg == "#282832"
    assert PI_DARK.tool_success_bg == "#283228"
    assert PI_DARK.tool_error_bg == "#3c2828"
    assert PI_DARK.thinking == "#808080"
    assert PI_DARK.planning == "#b294bb"


def test_startup_banner_keeps_pi_identity_hierarchy():
    async def run() -> None:
        app = PaiCliApp(cwd=".")
        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause()
            rendered = app.query_one("StartupBanner .banner-content").render()

            foregrounds = {span.style.foreground for span in rendered.spans}
            assert len(foregrounds) >= 3
            assert rendered.spans[0].style.bold is True

    asyncio.run(run())


def test_status_bar_prioritizes_context_and_model_on_narrow_terminals():
    async def run() -> None:
        app = PaiCliApp(cwd=".")
        async with app.run_test(size=(52, 20)) as pilot:
            await pilot.pause()
            status = app.query_one(StatusBar)
            status.session_text = "in 120.0k out 8.0k R70.0k W5.0k CH36.5% CNY0.42"
            status.context_text = "ctx 62.0k/128.0k 48%"
            status.pressure_text = "T1 55%"
            status.model = "qwen3.7-plus"

            lines = status.render().plain.splitlines()

            assert len(lines) == 2
            assert "ctx" in lines[1]
            assert "qwen3.7-plus" in lines[1]
            assert "CNY0.42" in lines[1]
            assert "W5.0k" not in lines[1]
            assert "CH36.5%" not in lines[1]

    asyncio.run(run())


def test_status_bar_reveals_usage_detail_as_terminal_width_allows():
    async def rendered_usage(width: int) -> str:
        app = PaiCliApp(cwd=".")
        async with app.run_test(size=(width, 20)) as pilot:
            await pilot.pause()
            status = app.query_one(StatusBar)
            status.session_text = "↑120.0k ↓8.0k R70.0k W5.0k CH36.5% ≈¥0.42"
            status.context_text = "ctx 62.0k/128.0k 48%"
            status.pressure_text = "pressure 55%"
            status.model = "qwen3.7"
            return status.render().plain.splitlines()[1]

    medium = asyncio.run(rendered_usage(80))
    wide = asyncio.run(rendered_usage(120))

    assert "R70.0k" in medium
    assert "W5.0k" not in medium
    assert "CH36.5%" not in medium
    assert "W5.0k" in wide
    assert "CH36.5%" in wide
