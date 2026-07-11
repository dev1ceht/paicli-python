from __future__ import annotations

import asyncio
from typing import Any

from paicli.agent import QueryEngine
from paicli.agent.agent import Agent
from paicli.config import load_config
from paicli.tools import ToolRegistry, get_builtin_tools
from paicli.tools.base import Tool, ToolResult
from paicli.types import Message


class FakeClient:
    model_name = "fake-model"
    provider_name = "fake-provider"
    max_context_window = 1000

    def __init__(self):
        self.calls = 0
        self.system_prompts: list[str] = []
        self.use_tool = True

    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        self.calls += 1
        self.system_prompts.append(system_prompt)
        if self.use_tool and self.calls == 1:
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": "call_1",
                    "function": {"name": "read_file", "arguments": '{"path":"note.txt"}'},
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
        else:
            tool_messages = [message for message in messages if message.role == "tool"]
            if self.use_tool:
                assert tool_messages
                assert "1: hello" in tool_messages[-1].content
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "message_end", "stop_reason": "end_turn"}


class FailingClient:
    model_name = "fake-model"
    provider_name = "fake-provider"
    max_context_window = 1000

    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        raise OSError("connection refused")
        yield  # pragma: no cover


class CapturingSummaryClient:
    model_name = "fake-model"
    provider_name = "fake-provider"
    max_context_window = 200

    def __init__(self):
        self.calls = 0
        self.messages_by_call = []

    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        self.calls += 1
        self.messages_by_call.append(list(messages))
        if self.calls == 1:
            yield {
                "type": "text_delta",
                "text": "## Goal\nSummarized old query history\n\n## Next Steps\nContinue.",
            }
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


class RepeatingToolClient:
    model_name = "fake-model"
    provider_name = "fake-provider"
    max_context_window = 1000

    def __init__(self):
        self.calls = 0
        self.tool_counts = []

    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        self.calls += 1
        self.tool_counts.append(len(tools))
        if not tools:
            yield {"type": "text_delta", "text": "final summary"}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        yield {
            "type": "tool_call_delta",
            "tool_call": {
                "index": 0,
                "id": f"call_{self.calls}",
                "function": {"name": "inspect", "arguments": "{}"},
            },
        }
        yield {"type": "message_end", "stop_reason": "tool_use"}


def test_query_engine_executes_tool_and_replays_result(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "note.txt").write_text("hello\n", encoding="utf-8")
    config = load_config(project_root=tmp_path)
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    engine = QueryEngine(
        llm_client=FakeClient(),
        tool_registry=registry,
        config=config,
        cwd=str(tmp_path),
    )

    async def run() -> Any:
        return await engine.ask_complete_async("read note")

    result = asyncio.run(run())
    assert result.text == "done"
    assert result.turns == 2


def test_query_engine_injects_relevant_long_term_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(project_root=tmp_path)
    config.memory.long_term_path = str(tmp_path / "memory" / "long_term_memory.json")
    from paicli.memory import MemoryManager

    MemoryManager(config.memory.long_term_path, project_path=tmp_path).save(
        "Chrome login reuse is allowed",
        scope="global",
    )
    client = FakeClient()
    client.use_tool = False
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    engine = QueryEngine(
        llm_client=client,
        tool_registry=registry,
        config=config,
        cwd=str(tmp_path),
    )

    async def run() -> Any:
        return await engine.ask_complete_async("Chrome login")

    asyncio.run(run())

    assert any("## 相关长期记忆" in prompt for prompt in client.system_prompts)
    assert any("Chrome login reuse is allowed" in prompt for prompt in client.system_prompts)


def test_query_engine_streams_llm_connection_failure_as_error_event(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(project_root=tmp_path)
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    engine = QueryEngine(
        llm_client=FailingClient(),
        tool_registry=registry,
        config=config,
        cwd=str(tmp_path),
    )

    async def run() -> list[dict[str, Any]]:
        return [event async for event in engine.ask("hello")]

    events = asyncio.run(run())

    assert events[-1]["type"] == "error"
    assert "调用 LLM 失败" in str(events[-1]["error"])
    assert "connection refused" in str(events[-1]["error"])


def test_query_engine_complete_still_raises_llm_connection_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(project_root=tmp_path)
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    engine = QueryEngine(
        llm_client=FailingClient(),
        tool_registry=registry,
        config=config,
        cwd=str(tmp_path),
    )

    async def run() -> Any:
        return await engine.ask_complete_async("hello")

    try:
        asyncio.run(run())
    except RuntimeError as exc:
        assert "调用 LLM 失败" in str(exc)
    else:
        raise AssertionError("expected LLM failure to raise in complete mode")


def test_agent_compacts_actual_messages_and_writes_back_history(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(project_root=tmp_path)
    config.context.min_budget_chars = 100
    config.context.max_budget_chars = 100
    config.context.output_reserve_tokens = 0
    config.context.protected_turns = 1
    old_secret = "OLD_QUERY_HISTORY"
    history = []
    for index in range(4):
        history.append(Message(role="user", content=f"{old_secret} user {index} " * 20))
        history.append(Message(role="assistant", content=f"{old_secret} assistant {index} " * 20))
    history.extend(
        [
            Message(role="user", content="recent query request"),
            Message(role="assistant", content="recent query response"),
        ]
    )
    client = CapturingSummaryClient()
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    agent = Agent(
        llm_client=client,
        tool_registry=registry,
        system_prompt="system prompt",
        cwd=str(tmp_path),
        config=config,
    )
    agent.history = history

    async def run() -> None:
        events = [event async for event in agent.run("current request " * 80)]
        assert events[-1]["type"] == "done"

    asyncio.run(run())

    actual_messages = "\n".join(
        str(message.content) for message in client.messages_by_call[-1]
    )
    assert "Summarized old query history" in actual_messages
    assert old_secret not in actual_messages
    written_history = "\n".join(str(message.content) for message in agent.history)
    assert "Summarized old query history" in written_history
    assert old_secret not in written_history


def test_query_engine_finalizes_without_tools_after_repeated_tool_batches(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(project_root=tmp_path)
    config.agent.stagnation_threshold = 3
    client = RepeatingToolClient()
    registry = ToolRegistry()

    async def inspect(_payload, _context):
        return ToolResult("unchanged")

    registry.register(Tool(name="inspect", description="", parameters={"type": "object"}, handler=inspect))
    engine = QueryEngine(llm_client=client, tool_registry=registry, config=config, cwd=str(tmp_path))

    result = asyncio.run(engine.ask_complete_async("inspect repeatedly"))

    assert result.text == "final summary"
    assert client.tool_counts == [1, 1, 1, 0]
