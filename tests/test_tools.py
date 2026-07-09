from __future__ import annotations

import asyncio

from paicli.config import load_config
from paicli.tools import ToolRegistry, get_builtin_tools
from paicli.tools.base import Tool, ToolContext


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
