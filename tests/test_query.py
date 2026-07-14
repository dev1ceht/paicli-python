from __future__ import annotations

import asyncio
from threading import Event
from typing import Any

import pytest

from paicli.agent import QueryEngine
from paicli.agent.agent import Agent
from paicli.cancellation import TaskCanceled
from paicli.config import LlmConfig, load_config
from paicli.retry import RetryPolicy
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


class ReasoningToolClient:
    model_name = "deepseek-v4-flash"
    provider_name = "deepseek"
    max_context_window = 1_000_000

    def __init__(self):
        self.calls = 0
        self.follow_up_messages: list[Message] = []

    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        self.calls += 1
        if self.calls == 1:
            yield {"type": "thinking_delta", "thinking": "先读取目标文件"}
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": "call_reasoning",
                    "function": {"name": "inspect", "arguments": "{}"},
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return
        self.follow_up_messages = list(messages)
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


class RetryingToolClient:
    model_name = "fake-model"
    provider_name = "fake-provider"
    max_context_window = 1000

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        self.calls += 1
        if self.calls == 1:
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": "call_retry",
                    "function": {"name": "remote_read", "arguments": "{}"},
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


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


def test_background_query_stops_at_a_cancellation_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PAICLI_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    config = load_config(project_root=tmp_path)
    signal = Event()
    signal.set()
    client = FakeClient()
    registry = ToolRegistry()
    engine = QueryEngine(
        llm_client=client,
        tool_registry=registry,
        config=config,
        cwd=str(tmp_path),
        cancellation_check=signal.is_set,
    )

    with pytest.raises(TaskCanceled):
        asyncio.run(engine.ask_complete_async("do work"))
    assert client.calls == 0


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
        assert any(event["type"] == "context_status" for event in events)

    asyncio.run(run())

    actual_messages = "\n".join(str(message.content) for message in client.messages_by_call[-1])
    assert "Summarized old query history" in actual_messages
    assert old_secret not in actual_messages
    written_history = "\n".join(str(message.content) for message in agent.history)
    assert "Summarized old query history" in written_history
    assert old_secret not in written_history


def test_agent_run_can_skip_history_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(project_root=tmp_path)
    client = FakeClient()
    client.use_tool = False
    agent = Agent(
        llm_client=client,
        tool_registry=ToolRegistry(),
        system_prompt="system prompt",
        cwd=str(tmp_path),
        config=config,
    )
    original_history = [Message(role="user", content="keep this context")]
    agent.history = list(original_history)

    async def run() -> None:
        events = [event async for event in agent.run("plan task", commit_history=False)]
        assert events[-1]["type"] == "done"

    asyncio.run(run())

    assert agent.history == original_history


def test_agent_reconfigure_llm_rebuilds_context_manager_and_preserves_history(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(project_root=tmp_path)
    config.features.skill = False
    registry = ToolRegistry()
    agent = Agent(
        llm_client=FakeClient(),
        tool_registry=registry,
        system_prompt="old prompt",
        cwd=str(tmp_path),
        config=config,
    )
    agent.history = [Message(role="user", content="keep this")]
    old_context_manager = agent.context_manager

    client = agent.reconfigure_llm(
        LlmConfig(provider="qwen", model="qwen-turbo", api_key="qwen-key")
    )

    assert agent.llm_client is client
    assert agent.context_manager is not old_context_manager
    assert agent.context_manager.llm_client is client
    assert agent.history == [Message(role="user", content="keep this")]
    assert config.llm.model == "qwen-turbo"
    assert "当前模型：qwen-turbo（qwen）" in agent.system_prompt

    async def live_tool(_payload, _context):
        return ToolResult("ok")

    registry.register(
        Tool(
            name="live_tool",
            description="MCP 动态工具",
            parameters={"type": "object"},
            handler=live_tool,
        )
    )
    next_turn_prompt = agent._system_prompt_for_message("next turn")
    assert "`live_tool`：MCP 动态工具" in next_turn_prompt


def test_query_engine_finalizes_without_tools_after_repeated_tool_batches(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(project_root=tmp_path)
    config.agent.stagnation_threshold = 3
    client = RepeatingToolClient()
    registry = ToolRegistry()

    async def inspect(_payload, _context):
        return ToolResult("unchanged")

    registry.register(
        Tool(name="inspect", description="", parameters={"type": "object"}, handler=inspect)
    )
    engine = QueryEngine(
        llm_client=client, tool_registry=registry, config=config, cwd=str(tmp_path)
    )

    result = asyncio.run(engine.ask_complete_async("inspect repeatedly"))

    assert result.text == "final summary"
    assert client.tool_counts == [1, 1, 1, 0]


def test_query_preserves_reasoning_between_tool_calling_turns(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(project_root=tmp_path)
    client = ReasoningToolClient()
    registry = ToolRegistry()

    async def inspect(_payload, _context):
        return ToolResult("inspected")

    registry.register(
        Tool(name="inspect", description="inspect", parameters={"type": "object"}, handler=inspect)
    )
    engine = QueryEngine(
        llm_client=client, tool_registry=registry, config=config, cwd=str(tmp_path)
    )

    result = asyncio.run(engine.ask_complete_async("inspect this"))

    assert result.text == "done"
    assistant_messages = [
        message for message in client.follow_up_messages if message.role == "assistant"
    ]
    assert assistant_messages[-1].reasoning_content == "先读取目标文件"


def test_query_streams_tool_retry_events_and_structured_result(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PAICLI_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    config = load_config(project_root=tmp_path)
    config.policy.audit_log_path = str(tmp_path / "audit")
    config.retry.default = RetryPolicy(base_delay=0.0, max_delay=0.0)
    attempts = 0

    async def remote_read(_payload, _context):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return ToolResult(
                "temporary timeout",
                is_error=True,
                error_kind="timeout",
                retryable=True,
            )
        return ToolResult("ok")

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="remote_read",
            description="read remote data",
            parameters={"type": "object"},
            handler=remote_read,
        )
    )
    engine = QueryEngine(
        llm_client=RetryingToolClient(),
        tool_registry=registry,
        config=config,
        cwd=str(tmp_path),
    )

    async def run() -> list[dict[str, Any]]:
        return [event async for event in engine.ask("read it")]

    events = asyncio.run(run())

    retry_event = next(event for event in events if event["type"] == "retry")
    result_event = next(event for event in events if event["type"] == "tool_result")
    assert retry_event["error_kind"] == "timeout"
    assert result_event["is_error"] is False
    assert result_event["error_kind"] is None
