from __future__ import annotations

import asyncio
import sys
from threading import Event

import pytest

from paicli.cancellation import TaskCanceled
from paicli.config import load_config
from paicli.policy import AuditLog
from paicli.retry import RetryPolicy
from paicli.tools import ToolRegistry, get_builtin_tools
from paicli.tools.base import Tool, ToolContext, ToolResult
from paicli.tools.executor import ToolExecutor


def test_bash_cannot_read_textual_terminal_input(tmp_path, monkeypatch):
    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "never"
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    context = ToolContext(cwd=str(tmp_path), config=config)
    captured_options = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"ok", b""

    async def create_process(_command, **options):
        captured_options.update(options)
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", create_process)

    async def run():
        tool = registry.get("bash")
        assert tool
        return await tool.execute({"command": "date"}, context)

    result = asyncio.run(run())

    assert result.content == "ok"
    assert captured_options["stdin"] is asyncio.subprocess.DEVNULL


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shell encoding regression")
def test_bash_decodes_windows_shell_output_without_replacement_characters(tmp_path):
    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "never"
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    context = ToolContext(cwd=str(tmp_path), config=config)
    command = (
        f'"{sys.executable}" -c "import sys;'
        "sys.stdout.buffer.write(bytes.fromhex('b5b1c7b0c8d5c6da'))\""
    )

    async def run():
        tool = registry.get("bash")
        assert tool
        return await tool.execute({"command": command}, context)

    result = asyncio.run(run())

    assert result.content == "当前日期"
    assert "\ufffd" not in result.content


def test_read_write_file_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "never"
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    context = ToolContext(cwd=str(tmp_path), config=config)

    async def run():
        write = registry.get("write_file")
        read = registry.get("read_file")
        assert write and read
        write_result = await write.execute(
            {"path": "hello.txt", "content": "hello\nworld\n"},
            context,
        )
        read_result = await read.execute({"path": "hello.txt"}, context)
        return write_result, read_result

    write_result, read_result = asyncio.run(run())
    assert not write_result.is_error
    assert "1: hello" in read_result.content
    assert "2: world" in read_result.content


def test_tool_registry_unregisters_prefix():
    async def handler(_payload, _context):
        return "ok"

    registry = ToolRegistry()
    registry.register(Tool(name="read_file", description="", parameters={}, handler=handler))
    registry.register(Tool(name="mcp__fake__echo", description="", parameters={}, handler=handler))
    registry.register(Tool(name="mcp__other__echo", description="", parameters={}, handler=handler))

    removed = registry.unregister_prefix("mcp__fake__")

    assert removed == 1
    assert registry.get("mcp__fake__echo") is None
    assert registry.get("mcp__other__echo") is not None


def test_save_memory_tool_accepts_fact_scope_and_legacy_content(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(project_root=tmp_path)
    config.memory.long_term_path = str(tmp_path / "memory" / "long_term_memory.json")
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    context = ToolContext(cwd=str(tmp_path), config=config)

    async def run():
        tool = registry.get("save_memory")
        assert tool
        first = await tool.execute(
            {"fact": "Always answer in Chinese", "scope": "global"},
            context,
        )
        second = await tool.execute({"content": "Project uses pytest"}, context)
        return first, second

    first, second = asyncio.run(run())

    assert not first.is_error
    assert "global" in first.content
    assert not second.is_error
    assert "project" in second.content


def test_executor_rejects_tool_arguments_that_violate_json_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(project_root=tmp_path)
    executed = False

    async def handler(_payload, _context):
        nonlocal executed
        executed = True
        raise AssertionError("invalid payload must not execute the tool")

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="bounded_search",
            description="",
            parameters={
                "type": "object",
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 5}},
                "required": ["limit"],
            },
            handler=handler,
        )
    )
    context = ToolContext(cwd=str(tmp_path), config=config)

    async def run():
        return await ToolExecutor(registry).execute_all(
            [
                {
                    "id": "call_invalid",
                    "function": {"name": "bounded_search", "arguments": '{"limit": 0}'},
                }
            ],
            context,
        )

    results = asyncio.run(run())

    assert results[0].is_error
    assert "minimum" in results[0].content
    assert not executed


def test_executor_allows_only_the_exact_tool_for_session(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(project_root=tmp_path)
    config.policy.audit_log_path = str(tmp_path / "audit")
    calls: list[str] = []

    async def handler(_payload, _context):
        calls.append("executed")
        from paicli.tools.base import ToolResult

        return ToolResult("ok")

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="first",
            description="",
            parameters={},
            handler=handler,
            is_read_only=False,
            requires_approval=True,
        )
    )
    registry.register(
        Tool(
            name="second",
            description="",
            parameters={},
            handler=handler,
            is_read_only=False,
            requires_approval=True,
        )
    )

    async def approve_once(request):
        return "allow_session" if request["tool_name"] == "first" else "deny"

    context = ToolContext(cwd=str(tmp_path), config=config, approval_callback=approve_once)

    async def run():
        executor = ToolExecutor(registry)
        first = await executor.execute_all([{"id": "1", "name": "first", "arguments": {}}], context)
        second = await executor.execute_all(
            [{"id": "2", "name": "second", "arguments": {}}], context
        )
        return first, second

    first, second = asyncio.run(run())
    assert not first[0].is_error
    assert second[0].is_error
    assert calls == ["executed"]
    assert context.session_allowed_tools == {"first"}


def test_executor_propagates_cancellation_without_executing_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(project_root=tmp_path)
    executed = False
    signal = Event()
    signal.set()

    async def handler(_payload, _context):
        nonlocal executed
        executed = True
        raise AssertionError("canceled task must not execute a tool")

    registry = ToolRegistry()
    registry.register(Tool(name="inspect", description="", parameters={}, handler=handler))
    context = ToolContext(cwd=str(tmp_path), config=config, cancellation_check=signal.is_set)

    async def run():
        await ToolExecutor(registry).execute_all(
            [{"id": "call_1", "name": "inspect", "arguments": {}}],
            context,
        )

    with pytest.raises(TaskCanceled):
        asyncio.run(run())
    assert not executed


def test_read_only_idempotent_tool_retries_structured_transient_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(project_root=tmp_path)
    config.policy.audit_log_path = str(tmp_path / "audit")
    attempts = 0
    events: list[dict] = []

    async def handler(_payload, _context):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return ToolResult(
                "temporary timeout",
                is_error=True,
                error_kind="timeout",
                retryable=True,
                retry_after=0.0,
            )
        return ToolResult("ok")

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="remote_read",
            description="",
            parameters={"type": "object"},
            handler=handler,
            is_read_only=True,
            is_idempotent=True,
        )
    )
    context = ToolContext(
        cwd=str(tmp_path),
        config=config,
        event_sink=events.append,
    )

    async def run():
        return await ToolExecutor(registry).execute_all(
            [{"id": "call_retry", "name": "remote_read", "arguments": {}}],
            context,
        )

    results = asyncio.run(run())

    assert attempts == 3
    assert results[0].content == "ok"
    assert [event["attempt"] for event in events if event["type"] == "retry"] == [1, 2]
    audit_events = AuditLog(config.policy.audit_log_path).tail(10)
    retry_events = [event for event in audit_events if event.get("event_type") == "retry"]
    assert [event["attempt"] for event in retry_events] == [1, 2]
    assert len({event["logical_call_id"] for event in retry_events}) == 1


def test_read_only_tool_retries_timeout_exceptions(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(project_root=tmp_path)
    config.policy.audit_log_path = str(tmp_path / "audit")
    attempts = 0

    async def handler(_payload, _context):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("remote read timed out")
        return ToolResult("ok")

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="timeout_read",
            description="",
            parameters={"type": "object"},
            handler=handler,
        )
    )
    context = ToolContext(cwd=str(tmp_path), config=config)

    async def run():
        return await ToolExecutor(registry).execute_all(
            [{"id": "timeout", "name": "timeout_read", "arguments": {}}], context
        )

    results = asyncio.run(run())

    assert attempts == 2
    assert results[0].content == "ok"


def test_executor_never_retries_non_idempotent_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(project_root=tmp_path)
    attempts = 0

    async def handler(_payload, _context):
        nonlocal attempts
        attempts += 1
        return ToolResult(
            "temporary timeout",
            is_error=True,
            error_kind="timeout",
            retryable=True,
        )

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="unsafe_read",
            description="",
            parameters={"type": "object"},
            handler=handler,
            is_read_only=True,
            is_idempotent=False,
        )
    )
    context = ToolContext(cwd=str(tmp_path), config=config)

    async def run():
        return await ToolExecutor(registry).execute_all(
            [{"id": "unsafe", "name": "unsafe_read", "arguments": {}}], context
        )

    results = asyncio.run(run())

    assert attempts == 1
    assert results[0].is_error


def test_read_only_tool_emits_and_audits_retry_exhaustion(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(project_root=tmp_path)
    config.policy.audit_log_path = str(tmp_path / "audit")
    config.retry.default = RetryPolicy(max_retries=1, base_delay=0, max_delay=0)
    attempts = 0
    events: list[dict] = []

    async def handler(_payload, _context):
        nonlocal attempts
        attempts += 1
        return ToolResult(
            "temporary timeout",
            is_error=True,
            error_kind="timeout",
            retryable=True,
        )

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="exhausted_read",
            description="",
            parameters={"type": "object"},
            handler=handler,
        )
    )
    context = ToolContext(cwd=str(tmp_path), config=config, event_sink=events.append)

    async def run():
        return await ToolExecutor(registry).execute_all(
            [{"id": "exhausted", "name": "exhausted_read", "arguments": {}}],
            context,
        )

    results = asyncio.run(run())

    assert attempts == 2
    assert results[0].is_error
    assert [event["type"] for event in events] == ["retry", "retry_exhausted"]
    audit_events = AuditLog(config.policy.audit_log_path).tail(10)
    assert [event["outcome"] for event in audit_events] == ["scheduled", "exhausted"]


def test_read_only_tool_does_not_retry_when_audit_write_fails(tmp_path, monkeypatch):
    config = load_config(project_root=tmp_path)
    config.policy.audit_log_path = str(tmp_path / "audit")
    attempts = 0
    events: list[dict] = []

    async def handler(_payload, _context):
        nonlocal attempts
        attempts += 1
        return ToolResult(
            "temporary timeout",
            is_error=True,
            error_kind="timeout",
            retryable=True,
        )

    def fail_audit(*_args, **_kwargs) -> None:
        raise OSError("audit unavailable")

    monkeypatch.setattr(AuditLog, "record_retry", fail_audit)
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="audit_required_read",
            description="",
            parameters={"type": "object"},
            handler=handler,
        )
    )
    context = ToolContext(cwd=str(tmp_path), config=config, event_sink=events.append)

    async def run():
        return await ToolExecutor(registry).execute_all(
            [{"id": "audit", "name": "audit_required_read", "arguments": {}}],
            context,
        )

    result = asyncio.run(run())[0]

    assert attempts == 1
    assert events == []
    assert result.is_error
    assert "audit unavailable" in result.content
