from __future__ import annotations

import asyncio

from paicli.agent.agent import Agent
from paicli.cancellation import CancellationCheck
from paicli.config import PaiCliConfig
from paicli.llm.base import LlmClient
from paicli.prompt import PromptAssembler
from paicli.tools.registry import ToolRegistry
from paicli.types import Message, QueryResult


class QueryEngine:
    def __init__(
        self,
        *,
        llm_client: LlmClient,
        tool_registry: ToolRegistry,
        config: PaiCliConfig,
        cwd: str,
        approval_callback=None,
        cancellation_check: CancellationCheck | None = None,
    ):
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.config = config
        self.cwd = cwd
        self.approval_callback = approval_callback
        self.cancellation_check = cancellation_check
        self.system_prompt = PromptAssembler(
            config=config,
            cwd=cwd,
            tool_names=tool_registry.list_names(),
            tool_summaries=tool_registry.summaries(),
            model=llm_client.model_name,
            provider=llm_client.provider_name,
        ).build()

    async def ask(
        self,
        message: str,
        history: list[Message] | None = None,
        *,
        execution_state: dict | None = None,
        checkpoint_callback=None,
    ):
        agent = Agent(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            system_prompt=self.system_prompt,
            cwd=self.cwd,
            config=self.config,
            approval_callback=self.approval_callback,
            cancellation_check=self.cancellation_check,
        )
        agent.history = list(history or [])
        async for event in agent.run(
            message,
            execution_state=execution_state,
            checkpoint_callback=checkpoint_callback,
        ):
            yield event

    async def ask_complete_async(
        self,
        message: str,
        history: list[Message] | None = None,
        *,
        execution_state: dict | None = None,
        checkpoint_callback=None,
    ) -> QueryResult:
        text = ""
        tokens = 0
        turns = 0
        async for event in self.ask(
            message,
            history,
            execution_state=execution_state,
            checkpoint_callback=checkpoint_callback,
        ):
            if event.get("type") == "text_delta":
                text += str(event.get("text") or "")
            elif event.get("type") == "error":
                raise event["error"]
            elif event.get("type") == "done":
                tokens = int(event.get("total_tokens") or 0)
                turns = int(event.get("total_turns") or 0)
        return QueryResult(text=text, total_tokens=tokens, turns=turns)

    def ask_complete(self, message: str, history: list[Message] | None = None) -> QueryResult:
        return asyncio.run(self.ask_complete_async(message, history))
