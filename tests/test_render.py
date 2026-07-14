from __future__ import annotations

from io import StringIO

from rich.console import Console

from paicli.config import PaiCliConfig
from paicli.entrypoints.repl import _bottom_toolbar, _interactive_renderer, _prompt_message
from paicli.render import RichRenderer, estimate_cost, format_cost, format_elapsed, format_tokens


def test_banner_renders_pi_home_layout():
    stream = StringIO()
    console = Console(file=stream, color_system=None, width=200)
    renderer = RichRenderer(console=console)

    renderer.banner(
        model="deepseek-v4-flash",
        provider="deepseek",
        cwd="/tmp/project",
        tools=12,
        version="0.1.0",
        api_key_configured=True,
        mcp_servers=1,
        skills=3,
        agents_files=2,
        hitl_mode="never",
    )

    output = stream.getvalue()
    assert "████████████" in output
    assert "  ██    ██" in output
    assert "PaiCLI v0.1.0" in output
    assert "Signed in API Key" in output
    assert "Workspace" in output


def test_prompt_message_keeps_status_and_input_together():
    prompt = _prompt_message(
        cwd="/tmp/project",
        model="deepseek-v4-flash",
        tools=12,
        agents_files=2,
        mcp_servers=1,
        skills=3,
        stats={"total_tokens": 13187, "context_ratio": 0.013, "has_usage": True},
    )
    plain = "".join(text for _style, text in prompt)

    assert "2 AGENTS.md files" in plain
    assert "1 MCP server" in plain
    assert "3 skills · Tools 12" in plain
    assert "YOLO" not in plain
    assert "Shift+Tab" not in plain
    assert "deepseek-v4-flash" in plain
    assert "ctx 1%" in plain
    assert "/tmp/project" in plain
    assert "\n\n* " in plain
    assert plain.endswith("\n* ")


def test_bottom_toolbar_uses_runtime_summary_segments():
    toolbar = _bottom_toolbar(
        "/Users/me/project",
        "deepseek-v4-flash",
        {
            "turns": 1,
            "total_tokens": 13187,
            "context_ratio": 0.013,
            "has_usage": True,
            "pressure_tier": "tier2_prune",
        },
    )

    assert ("class:toolbar.model", "deepseek-v4-flash") in toolbar
    assert ("class:toolbar.ctx.value", "1%") in toolbar
    assert ("class:toolbar.pressure", "pressure:T2") in toolbar
    assert ("class:toolbar.cwd.value", "/Users/me/project") in toolbar
    assert not any(text == " TURN " for _style, text in toolbar)
    assert not any("Token" in text for _style, text in toolbar)


def test_interactive_renderer_enables_live_markdown_for_inline_mode():
    config = PaiCliConfig(render_mode="inline")

    renderer = _interactive_renderer(config, context_window=1000)

    assert renderer._live_markdown is True
    assert renderer.toolbar_status()["context_ratio"] == 0


def test_interactive_renderer_disables_live_markdown_for_plain_mode():
    config = PaiCliConfig(render_mode="plain")

    renderer = _interactive_renderer(config)

    assert renderer._live_markdown is False


def test_text_deltas_render_as_markdown_on_turn_complete():
    stream = StringIO()
    console = Console(file=stream, color_system=None, width=120)
    renderer = RichRenderer(console=console, context_window=1000)

    renderer.handle({"type": "thinking_delta", "thinking": "需要先确认项目结构"})
    renderer.handle({"type": "text_delta", "text": "你好，我是 **Pai"})
    renderer.handle({"type": "text_delta", "text": "CLI**\n\n- `read_file`\n- **网页搜索**"})
    renderer.handle({"type": "usage", "usage": {"input_tokens": 250, "output_tokens": 50}})
    renderer.handle({"type": "turn_complete"})
    renderer.handle({"type": "done", "total_turns": 1, "total_tokens": 300})

    output = stream.getvalue()
    assert "思考过程" in output
    assert "需要先确认项目结构" in output
    assert "Final Output" in output
    assert "PaiCLI" in output
    assert "read_file" in output
    assert "网页搜索" in output
    assert "Run Summary" not in output
    assert "**PaiCLI**" not in output
    assert "`read_file`" not in output

    stats = renderer.toolbar_status()
    assert stats["turns"] == 1
    assert stats["input_tokens"] == 250
    assert stats["output_tokens"] == 50
    assert stats["total_tokens"] == 300
    assert stats["context_ratio"] == 0.25
    assert stats["has_usage"] is True


def test_interleaved_thinking_does_not_repeat_assistant_output_panels():
    stream = StringIO()
    console = Console(file=stream, color_system=None, width=120)
    renderer = RichRenderer(console=console)

    renderer.handle({"type": "text_delta", "text": "第一段"})
    renderer.handle({"type": "thinking_delta", "thinking": "中途补充思考"})
    renderer.handle({"type": "text_delta", "text": "第二段"})
    renderer.handle({"type": "turn_complete"})

    output = stream.getvalue()
    assert output.count("Assistant Output") == 0
    assert output.count("Final Output") == 1
    assert output.count("思考过程") == 1
    assert "第一段第二段" in output


def test_streaming_text_waits_for_turn_boundary_by_default():
    stream = StringIO()
    console = Console(file=stream, color_system=None, width=120, force_terminal=True)
    renderer = RichRenderer(console=console)

    renderer.handle({"type": "text_delta", "text": "chunk 1"})
    renderer.handle({"type": "text_delta", "text": "chunk 2"})

    assert "Assistant Output" not in stream.getvalue()
    renderer.handle({"type": "turn_complete"})
    assert stream.getvalue().count("Final Output") == 1


def test_tool_use_and_result_render_as_structured_panels():
    stream = StringIO()
    console = Console(file=stream, color_system=None, width=120)
    renderer = RichRenderer(console=console)

    renderer.handle({"type": "tool_call", "name": "list_dir", "input": {"path": "."}})
    renderer.handle(
        {
            "type": "tool_result",
            "name": "list_dir",
            "result": "README.md\nsrc/",
            "is_error": False,
        }
    )

    output = stream.getvalue()
    assert "Tool Use" in output
    assert "list_dir" in output
    assert '"path": "."' in output
    assert "Tool Result · list_dir · ok" in output
    assert "README.md" in output


def test_plan_review_events_render_summary_and_shortcuts():
    stream = StringIO()
    console = Console(file=stream, color_system=None, width=120)
    renderer = RichRenderer(console=console)

    renderer.handle({"type": "plan_generation_started", "goal": "做一个学生管理系统"})
    renderer.handle({"type": "plan_thinking", "thinking": "先拆分 CRUD 与测试任务"})
    renderer.handle({"type": "plan_review_summary", "summary": "计划摘要\n- 任务数: 3"})
    renderer.handle({"type": "plan_review_instructions"})

    output = stream.getvalue()
    assert "Plan-and-Execute" in output
    assert "? 使用 Plan-and-Execute" not in output
    assert "使用 Plan-and-Execute 模式" in output
    assert "规划思考" in output
    assert "计划摘要" in output
    assert "Enter" in output
    assert "Ctrl+O" in output
    assert "ESC" in output
    assert "I" in output


def test_start_run_resets_token_usage():
    stream = StringIO()
    console = Console(file=stream, color_system=None, width=120)
    renderer = RichRenderer(console=console, context_window=1000)
    renderer.set_provider("deepseek")

    renderer.handle({"type": "usage", "usage": {"input_tokens": 900, "output_tokens": 10}})
    first_cost = renderer.toolbar_status()["cost"]
    renderer.start_run()

    pending_stats = renderer.toolbar_status()
    assert pending_stats["input_tokens"] == 900
    assert pending_stats["output_tokens"] == 10
    assert pending_stats["context_ratio"] == 0.9

    renderer.handle({"type": "usage", "usage": {"input_tokens": 100, "output_tokens": 20}})
    renderer.handle({"type": "done", "total_turns": 1, "total_tokens": 120})

    assert "900" not in stream.getvalue()
    stats = renderer.toolbar_status()
    assert stats["input_tokens"] == 100
    assert stats["output_tokens"] == 20
    assert stats["total_tokens"] == 120
    assert stats["context_ratio"] == 0.1
    assert stats["cost"] > first_cost


def test_missing_usage_keeps_toolbar_tokens_unavailable():
    stream = StringIO()
    console = Console(file=stream, color_system=None, width=120)
    renderer = RichRenderer(console=console, context_window=1000)

    renderer.handle({"type": "done", "total_turns": 1, "total_tokens": 0})

    assert "Run Summary" not in stream.getvalue()
    toolbar = _bottom_toolbar("/tmp/project", "deepseek-v4-flash", renderer.toolbar_status())
    assert ("class:toolbar.model", "deepseek-v4-flash") in toolbar
    assert ("class:toolbar.ctx.value", "(0%)") in toolbar


# -- New tests for Java-style rendering enhancements --


def test_format_tokens_formats_large_numbers():
    assert format_tokens(0) == "0"
    assert format_tokens(999) == "999"
    assert format_tokens(1000) == "1.0k"
    assert format_tokens(51000) == "51.0k"
    assert format_tokens(1000000) == "1.0M"
    assert format_tokens(1234567) == "1.2M"


def test_format_elapsed_formats_seconds_and_milliseconds():
    assert format_elapsed(0.0) == "0ms"
    assert format_elapsed(0.250) == "250ms"
    assert format_elapsed(0.999) == "999ms"
    assert format_elapsed(1.0) == "1.0s"
    assert format_elapsed(1.5) == "1.5s"
    assert format_elapsed(12.345) == "12.3s"


def test_estimate_cost_returns_zero_for_small_usage():
    cost = estimate_cost("deepseek", 10, 5)
    assert cost < 0.0001


def test_estimate_cost_calculates_for_deepseek():
    cost = estimate_cost("deepseek", 100000, 10000)
    # 100k/1000 * 0.00014 + 10k/1000 * 0.00028 = 0.014 + 0.0028 = 0.0168
    assert abs(cost - 0.0168) < 0.0001


def test_estimate_cost_uses_default_for_unknown_provider():
    cost = estimate_cost("unknown", 10000, 1000)
    # default: 0.001 input + 0.002 output = 10*0.001 + 1*0.002 = 0.012
    assert abs(cost - 0.012) < 0.0001


def test_format_cost_formats_yuan():
    assert format_cost(0.0) == ""
    assert format_cost(0.0001) == ""
    assert format_cost(0.0123) == "¥0.0123"
    assert format_cost(1.2345) == "¥1.2345"


def test_tool_label_known_tools():
    from paicli.render.rich_renderer import _tool_label
    assert "📖 读取" in _tool_label("read_file", {"path": "/tmp/x.py"})
    assert "✏️ 写入" in _tool_label("write_file", {"path": "src/main.py"})
    assert "⚡ 执行命令" in _tool_label("bash", {"command": "ls"})
    assert "🌐 联网搜索" in _tool_label("web_search", {"query": "python"})
    assert "🔧 unknown_tool" == _tool_label("unknown_tool", {})


def test_tool_label_mcp_tools():
    from paicli.render.rich_renderer import _tool_label
    result = _tool_label("mcp__github__create_issue", {})
    assert "🔌 MCP github.create_issue" == result


def test_banner_workspace_panel_shows_model_and_hitl():
    stream = StringIO()
    console = Console(file=stream, color_system=None, width=200)
    renderer = RichRenderer(console=console)

    renderer.banner(
        model="deepseek-v4-flash",
        provider="deepseek",
        cwd="/tmp/project",
        tools=12,
        version="0.1.0",
        api_key_configured=True,
        mcp_servers=2,
        skills=3,
        agents_files=1,
        hitl_mode="auto",
    )

    output = stream.getvalue()
    assert "deepseek-v4-flash" in output
    assert "deepseek" in output
    assert "AUTO" in output
    assert "Tools 12" in output
    assert "MCP 2" in output
    assert "Skills 3" in output
    assert "Workspace" in output


def test_banner_workspace_panel_yolo_mode():
    stream = StringIO()
    console = Console(file=stream, color_system=None, width=200)
    renderer = RichRenderer(console=console)

    renderer.banner(
        model="test",
        provider="test",
        cwd="/tmp",
        tools=1,
        version="0.1.0",
        hitl_mode="never",
    )

    output = stream.getvalue()
    assert "YOLO" in output


def test_toolbar_status_includes_new_fields():
    stream = StringIO()
    console = Console(file=stream, color_system=None, width=120)
    renderer = RichRenderer(console=console, context_window=1000)
    renderer.set_provider("deepseek")

    renderer.handle({"type": "usage", "usage": {"input_tokens": 500, "output_tokens": 100}})
    renderer.handle({"type": "done", "total_turns": 1, "total_tokens": 600})

    stats = renderer.toolbar_status()
    assert "elapsed" in stats
    assert "cost" in stats
    assert "phase" in stats
    assert "cached_tokens" in stats
    assert stats["phase"] == "idle"
    assert stats["cached_tokens"] == 0


def test_toolbar_status_tracks_context_pressure_tier():
    renderer = RichRenderer(context_window=1_000)

    renderer.handle({"type": "context_status", "pressure_tier": "tier3_summary"})

    assert renderer.toolbar_status()["pressure_tier"] == "tier3_summary"


def test_toolbar_includes_token_details_and_cost():
    toolbar = _bottom_toolbar(
        "/tmp/project",
        "deepseek-v4-flash",
        {
            "input_tokens": 51000,
            "output_tokens": 2000,
            "cached_tokens": 14300,
            "total_tokens": 53000,
            "context_ratio": 0.053,
            "has_usage": True,
            "elapsed": 1.5,
            "cost": 0.0168,
            "phase": "idle",
            "provider": "deepseek",
        },
    )
    plain = "".join(text for _style, text in toolbar)

    assert "51.0k" in plain  # input tokens
    assert "2.0k" in plain  # output tokens
    assert "14.3k" in plain  # cached tokens
    assert "¥0.0168" in plain  # cost
    assert "1.5s" in plain  # elapsed


def test_toolbar_hides_cost_when_zero():
    toolbar = _bottom_toolbar(
        "/tmp/project",
        "deepseek-v4-flash",
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "cached_tokens": 0,
            "total_tokens": 15,
            "context_ratio": 0.001,
            "has_usage": True,
            "elapsed": 0.5,
            "cost": 0.0,
            "phase": "running",
            "provider": "deepseek",
        },
    )
    plain = "".join(text for _style, text in toolbar)
    assert "¥" not in plain


def test_toolbar_shows_phase_indicator():
    toolbar = _bottom_toolbar(
        "/tmp/project",
        "test-model",
        {"phase": "running", "has_usage": False},
    )
    plain = "".join(text for _style, text in toolbar)
    assert "RUNNING" in plain


def test_toolbar_shows_context_window_size():
    toolbar = _bottom_toolbar(
        "/tmp/project",
        "deepseek-v4-flash",
        {
            "input_tokens": 8700,
            "output_tokens": 200,
            "cached_tokens": 0,
            "total_tokens": 8900,
            "context_ratio": 0.0087,
            "context_window": 1000000,
            "has_usage": True,
            "elapsed": 1.5,
            "cost": 0.0015,
            "phase": "idle",
            "provider": "deepseek",
        },
    )
    plain = "".join(text for _style, text in toolbar)
    # used = input_tokens (last API call's prompt_tokens), not total_tokens
    assert "ctx 8.7k/1.0M (1%)" in plain


def test_diff_rendering_new_file():
    stream = StringIO()
    console = Console(file=stream, color_system=None, width=120)
    renderer = RichRenderer(console=console)

    renderer.handle({
        "type": "diff",
        "file_path": "new_file.py",
        "before": None,
        "after": "line1\nline2\nline3",
    })

    output = stream.getvalue()
    assert "new_file.py" in output
    assert "+ line1" in output
    assert "+ line2" in output
    assert "+ line3" in output


def test_diff_rendering_deleted_file():
    stream = StringIO()
    console = Console(file=stream, color_system=None, width=120)
    renderer = RichRenderer(console=console)

    renderer.handle({
        "type": "diff",
        "file_path": "old_file.py",
        "before": "old content\nold line2",
        "after": None,
    })

    output = stream.getvalue()
    assert "old_file.py" in output
    assert "- old content" in output
    assert "- old line2" in output


def test_diff_rendering_unchanged():
    stream = StringIO()
    console = Console(file=stream, color_system=None, width=120)
    renderer = RichRenderer(console=console)

    renderer.handle({
        "type": "diff",
        "file_path": "same.py",
        "before": "same content",
        "after": "same content",
    })

    output = stream.getvalue()
    assert "内容未变" in output


def test_diff_rendering_modified():
    stream = StringIO()
    console = Console(file=stream, color_system=None, width=120)
    renderer = RichRenderer(console=console)

    renderer.handle({
        "type": "diff",
        "file_path": "modified.py",
        "before": "line1\nline2\nline3",
        "after": "line1\nmodified\nline3",
    })

    output = stream.getvalue()
    assert "modified.py" in output
    assert "- line2" in output
    assert "+ modified" in output


def test_set_provider_and_phase():
    renderer = RichRenderer()
    renderer.set_provider("glm")
    renderer.set_phase("plan")
    assert renderer._provider == "glm"
    assert renderer._phase == "plan"


def test_start_run_sets_phase_to_running():
    renderer = RichRenderer()
    renderer.set_phase("idle")
    renderer.start_run()
    assert renderer._phase == "running"


def test_done_event_resets_phase_to_idle():
    stream = StringIO()
    console = Console(file=stream, color_system=None, width=120)
    renderer = RichRenderer(console=console, context_window=1000)
    renderer.start_run()

    renderer.handle({"type": "text_delta", "text": "hello"})
    renderer.handle({"type": "done", "total_turns": 1, "total_tokens": 100})

    assert renderer._phase == "idle"


def test_plan_events_set_phase():
    stream = StringIO()
    console = Console(file=stream, color_system=None, width=120)
    renderer = RichRenderer(console=console)

    renderer.handle({"type": "plan_generation_started", "goal": "test"})
    assert renderer._phase == "plan"

    renderer.handle({"type": "plan_cancelled"})
    assert renderer._phase == "idle"


def test_plan_completed_resets_phase():
    stream = StringIO()
    console = Console(file=stream, color_system=None, width=120)
    renderer = RichRenderer(console=console)

    renderer.handle({"type": "plan_started", "tasks": []})
    assert renderer._phase == "plan"

    renderer.handle({"type": "plan_completed", "results": {"t1": "done"}})
    assert renderer._phase == "idle"
    assert "计划执行完成" in stream.getvalue()


def test_answer_marker_on_final_output():
    stream = StringIO()
    console = Console(file=stream, color_system=None, width=120)
    renderer = RichRenderer(console=console)

    renderer.handle({"type": "text_delta", "text": "hello"})
    renderer.handle({"type": "turn_complete"})

    output = stream.getvalue()
    # The answer marker ▪ should appear before final output
    assert "▪" in output
