from __future__ import annotations

import asyncio
from threading import Event

import pytest

from paicli.cancellation import TaskCanceled
from paicli.config import load_config
from paicli.policy import AuditLog
from paicli.retry import RetryPolicy
from paicli.snapshot.checkpoint import SnapshotRecord, WorkspaceCheckpointCoordinator
from paicli.tools import ToolRegistry, get_builtin_tools
from paicli.tools.base import Tool, ToolContext, ToolResult
from paicli.tools.executor import ToolExecutor


def test_bash_uses_noninteractive_bash_without_terminal_input(tmp_path, monkeypatch):
    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "never"
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    context = ToolContext(cwd=str(tmp_path), config=config)
    captured_options = {}

    class FakeProcess:
        returncode = 0
        pid = 123

        class Stream:
            def __init__(self, chunks):
                self._chunks = iter(chunks)

            async def read(self, _size):
                return next(self._chunks, b"")

        stdout = Stream([b"ok", b""])
        stderr = Stream([b""])

        async def wait(self):
            return self.returncode

    captured_args = ()

    async def create_process(*args, **options):
        nonlocal captured_args
        captured_args = args
        captured_options.update(options)
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr("paicli.tools.command_runner.resolve_bash", lambda: "bash")

    async def run():
        tool = registry.get("bash")
        assert tool
        return await tool.execute({"command": "date"}, context)

    result = asyncio.run(run())

    assert result.content == "ok"
    assert captured_options["stdin"] is asyncio.subprocess.DEVNULL
    execute_tool = registry.get("bash")
    assert execute_tool
    assert captured_args == ("bash", "--noprofile", "--norc", "-c", "date")
    assert "Bash command" in execute_tool.description
    assert registry.get("bash") is not None


def test_bash_accepts_an_optional_timeout(tmp_path, monkeypatch):
    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "never"
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    context = ToolContext(cwd=str(tmp_path), config=config)
    command = "printf ok"

    class FakeProcess:
        returncode = 0
        pid = 123

        class Stream:
            async def read(self, _size):
                return b""

        stdout = Stream()
        stderr = Stream()

        async def wait(self):
            return self.returncode

    async def create_process(*_args, **_options):
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr("paicli.tools.command_runner.resolve_bash", lambda: "bash")

    async def run():
        tool = registry.get("bash")
        assert tool
        return await tool.execute({"command": command, "timeout": 5}, context)

    result = asyncio.run(run())

    assert not result.is_error


def test_read_write_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "never"
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    context = ToolContext(cwd=str(tmp_path), config=config)

    async def run():
        write = registry.get("write")
        read = registry.get("read")
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


def test_builtin_search_tools_have_one_canonical_name():
    names = {tool.name for tool in get_builtin_tools()}

    assert "ls" in names
    assert "list_dir" not in names
    assert "find" in names
    assert "grep" in names
    assert "glob_files" not in names
    assert "grep_code" not in names


def test_find_and_grep_execute_under_canonical_names(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    source = tmp_path / "src" / "sample.py"
    source.parent.mkdir()
    source.write_text("TODO: verify canonical tools\n", encoding="utf-8")
    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "never"
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    context = ToolContext(cwd=str(tmp_path), config=config)

    async def run():
        find_tool = registry.get("find")
        grep_tool = registry.get("grep")
        assert find_tool and grep_tool
        find_result = await find_tool.execute({"pattern": "src/**/*.py"}, context)
        grep_result = await grep_tool.execute(
            {"pattern": "TODO", "path": "src", "regex": False}, context
        )
        return find_result, grep_result

    find_result, grep_result = asyncio.run(run())
    assert not find_result.is_error
    assert "sample.py" in find_result.content
    assert not grep_result.is_error
    assert "sample.py" in grep_result.content
    assert "TODO" in grep_result.content


def test_read_default_bounds_rendered_output(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    target = tmp_path / "large.txt"
    target.write_text("\n".join("x" * 100 for _ in range(1000)), encoding="utf-8")
    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "never"
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    context = ToolContext(cwd=str(tmp_path), config=config)

    async def run():
        tool = registry.get("read")
        assert tool
        return await tool.execute({"path": "large.txt"}, context)

    result = asyncio.run(run())
    body = result.content.split("\n\n[Output truncated", 1)[0]

    assert "[Output truncated" in result.content
    assert len(body.encode()) <= 50 * 1024


def test_write_overwrites_existing_file_and_creates_parent_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    target = tmp_path / "nested" / "module.py"
    target.parent.mkdir()
    target.write_text("VALUE = 1\n", encoding="utf-8")
    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "never"
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    context = ToolContext(cwd=str(tmp_path), config=config)

    async def run():
        tool = registry.get("write")
        assert tool
        return await tool.execute(
            {"path": "nested/module.py", "content": "VALUE = 2\n"},
            context,
        )

    result = asyncio.run(run())

    assert not result.is_error
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"


def test_edit_replaces_multiple_unique_nonoverlapping_text_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    target = tmp_path / "module.py"
    target.write_text("before\nVALUE = 1\nmiddle\nVALUE = 2\nafter\n", encoding="utf-8")
    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "never"
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    context = ToolContext(cwd=str(tmp_path), config=config)

    async def run():
        tool = registry.get("edit")
        assert tool
        return await tool.execute(
            {
                "path": "module.py",
                "edits": [
                    {"oldText": "VALUE = 1", "newText": "VALUE = 10"},
                    {"oldText": "VALUE = 2", "newText": "VALUE = 20"},
                ],
            },
            context,
        )

    result = asyncio.run(run())

    assert not result.is_error
    assert target.read_text(encoding="utf-8") == "before\nVALUE = 10\nmiddle\nVALUE = 20\nafter\n"


def test_edit_preserves_crlf_line_endings(tmp_path):
    target = tmp_path / "module.py"
    target.write_bytes(b"before\r\nVALUE = 1\r\nafter\r\n")
    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "never"
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    context = ToolContext(cwd=str(tmp_path), config=config)

    async def run():
        tool = registry.get("edit")
        assert tool
        return await tool.execute(
            {
                "path": "module.py",
                "edits": [{"oldText": "VALUE = 1", "newText": "VALUE = 2"}],
            },
            context,
        )

    result = asyncio.run(run())

    assert not result.is_error
    assert target.read_bytes() == b"before\r\nVALUE = 2\r\nafter\r\n"


def test_edit_rejects_an_ambiguous_text_block(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    target = tmp_path / "module.py"
    target.write_text("same\nsame\n", encoding="utf-8")
    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "never"
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    context = ToolContext(cwd=str(tmp_path), config=config)

    async def run():
        tool = registry.get("edit")
        assert tool
        return await tool.execute(
            {"path": "module.py", "edits": [{"oldText": "same", "newText": "changed"}]},
            context,
        )

    result = asyncio.run(run())

    assert result.is_error
    assert "matched 2 times" in result.content
    assert target.read_text(encoding="utf-8") == "same\nsame\n"


def test_edit_rejects_overlapping_replacements_without_writing(tmp_path, monkeypatch):
    target = tmp_path / "module.py"
    target.write_text("abcdef\n", encoding="utf-8")
    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "never"
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    context = ToolContext(cwd=str(tmp_path), config=config)

    async def run():
        tool = registry.get("edit")
        assert tool
        return await tool.execute(
            {
                "path": "module.py",
                "edits": [
                    {"oldText": "abc", "newText": "ABC"},
                    {"oldText": "bcd", "newText": "BCD"},
                ],
            },
            context,
        )

    result = asyncio.run(run())
    assert result.is_error
    assert "overlap" in result.content
    assert target.read_text(encoding="utf-8") == "abcdef\n"


def test_apply_patch_updates_existing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    target = tmp_path / "module.py"
    target.write_text("before\nVALUE = 1\nafter\n", encoding="utf-8")
    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "never"
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    context = ToolContext(cwd=str(tmp_path), config=config)
    patch = """*** Begin Patch
*** Update File: module.py
@@
-VALUE = 1
+VALUE = 2
*** End Patch"""

    async def run():
        tool = registry.get("apply_patch")
        assert tool
        return await tool.execute({"patch": patch}, context)

    result = asyncio.run(run())

    assert not result.is_error
    assert target.read_text(encoding="utf-8") == "before\nVALUE = 2\nafter\n"


def test_apply_patch_adds_moves_and_deletes_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "old.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "obsolete.py").write_text("obsolete\n", encoding="utf-8")
    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "never"
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    context = ToolContext(cwd=str(tmp_path), config=config)
    patch = """*** Begin Patch
*** Add File: created.py
+CREATED = True
*** Update File: old.py
*** Move to: moved.py
@@
-VALUE = 1
+VALUE = 2
*** Delete File: obsolete.py
*** End Patch"""

    async def run():
        tool = registry.get("apply_patch")
        assert tool
        return await tool.execute({"patch": patch}, context)

    result = asyncio.run(run())

    assert not result.is_error
    assert (tmp_path / "created.py").read_text(encoding="utf-8") == "CREATED = True\n"
    assert (tmp_path / "moved.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert not (tmp_path / "old.py").exists()
    assert not (tmp_path / "obsolete.py").exists()


def test_apply_patch_dry_run_validates_without_changing_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    target = tmp_path / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "never"
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    context = ToolContext(cwd=str(tmp_path), config=config)
    patch = """*** Begin Patch
*** Update File: module.py
@@
-VALUE = 1
+VALUE = 2
*** End Patch"""

    async def run():
        tool = registry.get("apply_patch")
        assert tool
        return await tool.execute({"patch": patch, "dry_run": True}, context)

    result = asyncio.run(run())

    assert not result.is_error
    assert result.content.startswith("Validated patch")
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_tool_registry_unregisters_prefix():
    async def handler(_payload, _context):
        return "ok"

    registry = ToolRegistry()
    registry.register(Tool(name="custom_read", description="", parameters={}, handler=handler))
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


def test_executor_creates_one_workspace_checkpoint_before_mutations(tmp_path):
    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "never"
    checkpoint_calls: list[str] = []

    class RecordingStore:
        def create(self, phase):
            checkpoint_calls.append(phase)
            return SnapshotRecord(
                id="checkpoint",
                phase=phase,
                created_at="now",
                path=tmp_path,
                backend="test",
            )

    async def handler(_payload, _context):
        return ToolResult("ok")

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="mutate",
            description="",
            parameters={"type": "object"},
            handler=handler,
            is_read_only=False,
            mutates_workspace=True,
        )
    )
    context = ToolContext(
        cwd=str(tmp_path),
        config=config,
        workspace_checkpoint=WorkspaceCheckpointCoordinator(RecordingStore()),
    )

    async def run():
        return await ToolExecutor(registry).execute_all(
            [
                {"id": "1", "name": "mutate", "arguments": {}},
                {"id": "2", "name": "mutate", "arguments": {}},
            ],
            context,
        )

    results = asyncio.run(run())

    assert [result.content for result in results] == ["ok", "ok"]
    assert checkpoint_calls == ["before-agent-edit"]


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
