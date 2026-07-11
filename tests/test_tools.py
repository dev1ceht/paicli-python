from __future__ import annotations

import asyncio

from paicli.config import load_config
from paicli.tools import ToolRegistry, get_builtin_tools
from paicli.tools.base import Tool, ToolContext
from paicli.tools.executor import ToolExecutor


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
