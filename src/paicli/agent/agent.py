from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

from paicli.cancellation import CancellationCheck, TaskCanceled, raise_if_cancelled
from paicli.config import LlmConfig, PaiCliConfig
from paicli.context import ContextManager
from paicli.llm import create_llm_client
from paicli.llm.base import LlmClient
from paicli.memory import MemoryManager
from paicli.prompt import PromptAssembler
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
        max_turns: int | None = None,
        cancellation_check: CancellationCheck | None = None,
    ):
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.system_prompt = system_prompt
        self.cwd = cwd
        self.config = config
        self.approval_callback = approval_callback
        self.max_turns = max_turns if max_turns is not None else config.agent.max_turns
        self.cancellation_check = cancellation_check
        self.history: list[Message] = []
        self.session_allowed_tools: set[str] = set()

        # 初始化上下文管理器
        self.context_manager = ContextManager(
            config=config,
            llm_client=llm_client,
            cwd=cwd,
        )

    async def run(
        self,
        message: str,
        *,
        execution_state: dict[str, Any] | None = None,
        checkpoint_callback=None,
    ) -> AsyncIterator[dict[str, Any]]:
        raise_if_cancelled(self.cancellation_check)
        snapshot = SnapshotService(self.cwd)
        canceled = False
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
                session_allowed_tools=self.session_allowed_tools,
                max_turns=self.max_turns,
                context_manager=self.context_manager,
                cancellation_check=self.cancellation_check,
                execution_state=execution_state,
                checkpoint_callback=checkpoint_callback,
            ):
                if event.get("type") == "done":
                    self.history = list(event.get("messages") or [])
                yield event
        except TaskCanceled:
            canceled = True
            raise
        finally:
            if not canceled:
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

    def reconfigure_llm(self, llm_config: LlmConfig) -> LlmClient:
        """Replace the idle session's client while retaining its conversation history."""
        client = create_llm_client(llm_config)
        self.config.llm = llm_config
        self.llm_client = client
        self.system_prompt = self._build_system_prompt()
        self.context_manager = ContextManager(
            config=self.config,
            llm_client=client,
            cwd=self.cwd,
        )
        return client

    def _system_prompt_for_message(self, message: str) -> str:
        memory_context = self._memory_context_for_message(message)
        self.system_prompt = self._build_system_prompt(relevant_memory=memory_context)
        return self.system_prompt

    def _build_system_prompt(self, *, relevant_memory: str = "") -> str:
        return PromptAssembler(
            config=self.config,
            cwd=self.cwd,
            tool_names=self.tool_registry.list_names(),
            tool_summaries=self.tool_registry.summaries(),
            model=self.llm_client.model_name,
            provider=self.llm_client.provider_name,
        ).build(relevant_memory=relevant_memory)

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
