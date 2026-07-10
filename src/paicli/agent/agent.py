from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

from paicli.config import PaiCliConfig
from paicli.context import ContextManager
from paicli.llm.base import LlmClient
from paicli.memory import MemoryManager
from paicli.snapshot import SnapshotService
from paicli.tools.registry import ToolRegistry
from paicli.types import Message, QueryResult

from .query import query


class Agent:
    def __init__(
        self,
        *,
        llm_client: LlmClient,
        tool_registry: ToolRegistry,
        system_prompt: str,
        cwd: str,
        config: PaiCliConfig,
        approval_callback=None,
        max_turns: int = 20,
    ):
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.system_prompt = system_prompt
        self.cwd = cwd
        self.config = config
        self.approval_callback = approval_callback
        self.max_turns = max_turns
        self.history: list[Message] = []
        
        # 初始化上下文管理器
        self.context_manager = ContextManager(
            config=config,
            llm_client=llm_client,
            cwd=cwd,
        )

    async def run(self, message: str) -> AsyncIterator[dict[str, Any]]:
        snapshot = SnapshotService(self.cwd)
        with suppress(Exception):
            snapshot.create("pre-turn")
        try:
            system_prompt = self._system_prompt_for_message(message)
            async for event in query(
                llm_client=self.llm_client,
                tool_registry=self.tool_registry,
                system_prompt=system_prompt,
                user_message=message,
                history=self.history,
                cwd=self.cwd,
                config=self.config,
                approval_callback=self.approval_callback,
                max_turns=self.max_turns,
                context_manager=self.context_manager,
            ):
                if event.get("type") == "done":
                    self.history = list(event.get("messages") or [])
                yield event
        finally:
            with suppress(Exception):
                snapshot.create("post-turn")

    async def run_complete(self, message: str) -> QueryResult:
        text = ""
        tokens = 0
        turns = 0
        async for event in self.run(message):
            if event.get("type") == "text_delta":
                text += str(event.get("text") or "")
            elif event.get("type") == "error":
                raise event["error"]
            elif event.get("type") == "done":
                tokens = int(event.get("total_tokens") or 0)
                turns = int(event.get("total_turns") or 0)
        return QueryResult(text=text, total_tokens=tokens, turns=turns)

    def clear_history(self) -> None:
        self.history = []

    def _system_prompt_for_message(self, message: str) -> str:
        memory_context = self._memory_context_for_message(message)
        if not memory_context:
            return self.system_prompt
        return f"{self.system_prompt}\n\n{memory_context.strip()}"

    def _memory_context_for_message(self, message: str) -> str:
        if not self.config.features.memory or not self.config.memory.long_term_enabled:
            return ""
        budget = _memory_context_token_budget(self.llm_client.max_context_window)
        try:
            manager = MemoryManager(
                self.config.memory.long_term_path,
                project_path=self.cwd,
            )
            return manager.build_context_for_query(message, max_tokens=budget)
        except Exception:
            return ""


def _memory_context_token_budget(context_window: int | None) -> int:
    if not context_window:
        return 2000
    return min(5000, max(2000, int(context_window * 0.005)))
