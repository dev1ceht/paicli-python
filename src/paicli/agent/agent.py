from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from copy import deepcopy
from typing import Any

from paicli.cancellation import CancellationCheck, TaskCanceled, raise_if_cancelled
from paicli.config import LlmConfig, PaiCliConfig
from paicli.context import ContextManager
from paicli.context.telemetry import use_context_scope
from paicli.llm import create_llm_client
from paicli.llm.base import LlmClient
from paicli.memory import MemoryManager
from paicli.prompt import PromptAssembler, PromptSections
from paicli.snapshot import SnapshotService
from paicli.tools.registry import ToolRegistry
from paicli.types import Message, QueryResult

from .query import query


async def _run_blocking(callback: Callable[..., Any], *args: Any) -> Any:
    """Run blocking lifecycle work without freezing the caller's event loop."""
    task = asyncio.create_task(asyncio.to_thread(callback, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Cancellation cannot stop a worker thread. Let it settle before the
        # post-turn snapshot can touch the same side repository.
        await task
        raise


def _serialize_message(message: Message) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": message.content,
        "name": message.name,
        "tool_call_id": message.tool_call_id,
        "tool_calls": list(message.tool_calls),
        "reasoning_content": message.reasoning_content,
    }


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
        context_manager_factory: Callable[..., ContextManager] | None = None,
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
        self._session_checkpoint_marker: Any = None
        self._session_checkpoint: dict[str, Any] | None = None

        # 初始化上下文管理器
        manager_factory = context_manager_factory or ContextManager
        self.context_manager = manager_factory(config=config, llm_client=llm_client, cwd=cwd)

    async def run(
        self,
        message: str,
        *,
        commit_history: bool = True,
        context_scope: str = "agent",
        execution_state: dict[str, Any] | None = None,
        checkpoint_callback=None,
    ) -> AsyncIterator[dict[str, Any]]:
        raise_if_cancelled(self.cancellation_check)
        snapshot = await _run_blocking(SnapshotService, self.cwd)
        canceled = False
        context_checkpoint = self.context_manager.checkpoint_state()
        context_committed = False
        with suppress(Exception):
            await _run_blocking(snapshot.create, "pre-turn")
        try:
            prompt_sections = await _run_blocking(self._prompt_sections_for_message, message)
            system_prompt = await _run_blocking(prompt_sections.render)
            self.system_prompt = system_prompt
            with use_context_scope(context_scope):
                async for event in query(
                    llm_client=self.llm_client,
                    tool_registry=self.tool_registry,
                    system_prompt=system_prompt,
                    prompt_sections=prompt_sections,
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
                    if event.get("type") == "done" and commit_history:
                        self.history = list(event.get("messages") or [])
                        context_committed = True
                        baseline = self.context_usage_event()
                        if baseline:
                            yield baseline
                    yield event
        except TaskCanceled:
            canceled = True
            raise
        finally:
            if not context_committed:
                self.context_manager.restore_state(context_checkpoint)
            if not canceled:
                with suppress(Exception):
                    await _run_blocking(snapshot.create, "post-turn")

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
        self.replace_history([])

    def replace_history(self, history: list[Message]) -> None:
        """Replace durable model history and reset derived context state."""
        self.history = list(history)
        self._session_checkpoint_marker = None
        self._session_checkpoint = None
        self.context_manager.reset()

    def export_session_context(self) -> dict[str, Any] | None:
        """Export model history and compaction state for durable session replay."""
        durable_state = self.context_manager.export_durable_state()
        if durable_state is None:
            self._session_checkpoint_marker = None
            self._session_checkpoint = None
            return None
        marker = self.context_manager.checkpoint_state()[2]
        if marker is self._session_checkpoint_marker and self._session_checkpoint is not None:
            return deepcopy(self._session_checkpoint)
        messages = [_serialize_message(message) for message in self.history]
        identity = {
            "provider": self.config.llm.provider,
            "model": self.config.llm.model,
            "context": durable_state,
            "messages": messages,
        }
        digest = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        checkpoint = {
            "checkpoint_id": f"ctx_{digest[:24]}",
            "provider": self.config.llm.provider,
            "model": self.config.llm.model,
            **durable_state,
            "messages": messages,
        }
        self._session_checkpoint_marker = marker
        self._session_checkpoint = deepcopy(checkpoint)
        return checkpoint

    def restore_session_context(
        self,
        history: list[Message],
        context_checkpoint: dict[str, Any],
    ) -> None:
        """Restore a durable compacted history without discarding context metadata."""
        self.history = list(history)
        self.context_manager.restore_durable_state(context_checkpoint)
        self._session_checkpoint_marker = self.context_manager.checkpoint_state()[2]
        self._session_checkpoint = deepcopy(context_checkpoint)

    def close(self) -> None:
        self.context_manager.close()

    def context_usage_event(self) -> dict[str, Any] | None:
        build_event = getattr(self.llm_client, "context_usage_event", None)
        if not callable(build_event):
            return None
        try:
            return build_event(
                self.history,
                self.tool_registry.definitions(),
                self.system_prompt,
                state="retained",
                quality_budget_tokens=self.context_manager.quality_budget_tokens(),
                pressure_thresholds=self.context_manager.pressure_thresholds(),
            )
        except TypeError:
            return build_event(
                self.history,
                self.tool_registry.definitions(),
                self.system_prompt,
                state="retained",
            )

    def refresh_context_usage_event(self) -> dict[str, Any] | None:
        """Rebuild tool-dependent instructions and report the new idle baseline."""
        self.system_prompt = self._build_system_prompt()
        return self.context_usage_event()

    def reconfigure_llm(self, llm_config: LlmConfig) -> LlmClient:
        """Replace the idle session's client while retaining its conversation history."""
        client = create_llm_client(
            llm_config,
            retry_policy=self.config.retry.resolve("llm"),
            retry_audit_path=self.config.policy.audit_log_path,
            retry_cwd=self.cwd,
        )
        self.config.llm = llm_config
        self.llm_client = client
        self.system_prompt = self._build_system_prompt()
        self.context_manager.llm_client = client
        return client

    def _prompt_sections_for_message(self, message: str) -> PromptSections:
        memory_context = self._memory_context_for_message(message)
        return self._build_prompt_sections(relevant_memory=memory_context)

    def _system_prompt_for_message(self, message: str) -> str:
        """Compatibility wrapper for callers that still need the rendered prompt."""
        sections = self._prompt_sections_for_message(message)
        self.system_prompt = sections.render()
        return self.system_prompt

    def _build_system_prompt(self, *, relevant_memory: str = "") -> str:
        return self._build_prompt_sections(relevant_memory=relevant_memory).render()

    def _build_prompt_sections(self, *, relevant_memory: str = "") -> PromptSections:
        return PromptAssembler(
            config=self.config,
            cwd=self.cwd,
            tool_names=self.tool_registry.list_names(),
            tool_summaries=self.tool_registry.summaries(),
            model=self.llm_client.model_name,
            provider=self.llm_client.provider_name,
        ).build_sections(relevant_memory=relevant_memory)

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
