from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from paicli.config import PaiCliConfig
from paicli.context.assembler import (
    AssembledPrompt,
    Section,
    SectionType,
    assemble_prompt,
)
from paicli.context.budget import Budget, calculate_budget
from paicli.context.compaction import (
    CompactionResult,
    DeltaItem,
    compact_with_llm,
    deterministic_compact,
    extract_delta_items,
)
from paicli.context.pressure import (
    PressureResult,
    PressureTier,
    apply_overflow_fallback,
    apply_pressure_tier,
    calculate_pressure,
    should_trigger_compaction,
)
from paicli.context.token_estimator import (
    TokenEstimator,
    calibrate_estimation,
    estimate_tokens,
    get_calibration_factor,
)
from paicli.context.tool_result import (
    apply_tool_result_compression,
    compress_old_tool_results,
    offload_large_tool_results,
)
from paicli.llm.base import LlmClient
from paicli.types import Message


@dataclass
class ContextBuildResult:
    system_prompt: str
    messages: list[Message]
    compacted: bool = False
    pressure_tier: str | None = None


@dataclass
class ContextManager:
    """Build prompts and keep the actual message list under control."""

    config: PaiCliConfig
    llm_client: LlmClient
    cwd: str
    session_id: str = "default"

    _current_summary: str = ""
    _token_estimator: TokenEstimator = field(default_factory=TokenEstimator)
    _last_pressure: PressureResult | None = None
    _last_compaction: CompactionResult | None = None

    def build_prompt(
        self,
        *,
        prefix: str = "",
        memory: str = "",
        skills: str = "",
        relevant_memory: str = "",
        history: list[Message] | None = None,
        current_request: str = "",
        actual_usage: dict[str, int] | None = None,
    ) -> str:
        """Build a system prompt without performing async history compaction."""
        assembled, budget, _pressure = self._assemble_prompt(
            prefix=prefix,
            memory=memory,
            skills=skills,
            relevant_memory=relevant_memory,
            history=history or [],
            current_request=current_request,
            actual_usage=actual_usage,
        )
        if assembled.total_tokens > budget.prompt_tokens:
            assembled = apply_overflow_fallback(assembled, budget)
        return assembled.to_string()

    async def build_turn_context(
        self,
        *,
        prefix: str = "",
        memory: str = "",
        skills: str = "",
        relevant_memory: str = "",
        messages: list[Message] | None = None,
        actual_usage: dict[str, int] | None = None,
    ) -> ContextBuildResult:
        all_messages = list(messages or [])
        history, current = _split_current_request(all_messages)
        current_request = _message_content(current) if current else ""
        history = self._compress_tool_results(history)

        assembled, budget, pressure = self._assemble_prompt(
            prefix=prefix,
            memory=memory,
            skills=skills,
            relevant_memory=relevant_memory,
            history=history,
            current_request=current_request,
            actual_usage=actual_usage,
        )

        compacted = False
        output_messages = [*history, *([current] if current else [])]
        if (
            self.config.features.context_compression
            and should_trigger_compaction(pressure, len(history))
        ):
            compacted_messages = await self._compact_messages(history)
            if compacted_messages is not None:
                compacted = True
                output_messages = [*compacted_messages, *([current] if current else [])]
                assembled, budget, pressure = self._assemble_prompt(
                    prefix=prefix,
                    memory=memory,
                    skills=skills,
                    relevant_memory=relevant_memory,
                    history=compacted_messages,
                    current_request=current_request,
                    actual_usage=None,
                )

        if assembled.total_tokens > budget.prompt_tokens:
            assembled = apply_overflow_fallback(assembled, budget)

        return ContextBuildResult(
            system_prompt=assembled.to_string(),
            messages=output_messages,
            compacted=compacted,
            pressure_tier=pressure.tier.value if pressure else None,
        )

    def _assemble_prompt(
        self,
        *,
        prefix: str = "",
        memory: str = "",
        skills: str = "",
        relevant_memory: str = "",
        history: list[Message] | None = None,
        current_request: str = "",
        actual_usage: dict[str, int] | None = None,
    ) -> tuple[AssembledPrompt, Budget, PressureResult]:
        if actual_usage:
            estimated = self._token_estimator.estimate(
                prefix + memory + skills + relevant_memory + current_request
            )
            actual = actual_usage.get("input_tokens", 0)
            if actual > 0:
                self._token_estimator.calibrate(estimated, actual)

        budget = self._calculate_budget()
        assembled = assemble_prompt(
            prefix=prefix,
            memory=memory,
            skills=skills,
            relevant_memory=relevant_memory,
            history=history or [],
            current_request=current_request,
            budget=budget,
            keep_recent_tool_results=self.config.context.tool_result_keep_recent,
            max_tool_result_bytes=self.config.context.tool_result_max_total_bytes,
            tool_result_preview_chars=self.config.context.tool_result_preview_chars,
            tool_result_storage_dir=self.config.context.tool_result_storage_dir,
            session_id=self.session_id,
        )
        pressure = calculate_pressure(assembled, budget)
        self._last_pressure = pressure
        assembled = apply_pressure_tier(assembled, pressure)
        return assembled, budget, pressure

    def _compress_tool_results(self, messages: list[Message]) -> list[Message]:
        return apply_tool_result_compression(
            messages,
            keep_recent=self.config.context.tool_result_keep_recent,
            max_total_bytes=self.config.context.tool_result_max_total_bytes,
            preview_chars=self.config.context.tool_result_preview_chars,
            storage_dir=self.config.context.tool_result_storage_dir,
            session_id=self.session_id,
        )

    async def _compact_messages(self, history: list[Message]) -> list[Message] | None:
        delta_items, protected_items = extract_delta_items(
            history,
            protected_turns=self.config.context.protected_turns,
        )
        if not delta_items:
            return None

        try:
            compaction_result = await compact_with_llm(
                delta_items,
                self.llm_client,
                prior_summary=self._current_summary,
            )
        except Exception:
            compaction_result = deterministic_compact(
                delta_items,
                prior_summary=self._current_summary,
            )

        compaction_result.protected_items = len(protected_items)
        self._last_compaction = compaction_result
        self._current_summary = compaction_result.summary
        return [
            Message(
                role="system",
                content=f"[Previous conversation summary]\n{compaction_result.summary}",
            ),
            *_messages_from_delta_items(protected_items),
        ]

    def _calculate_budget(self) -> Budget:
        return calculate_budget(
            context_window=self.llm_client.max_context_window,
            utilization_rate=self.config.context.utilization_rate,
            output_reserve_tokens=self.config.context.output_reserve_tokens,
            min_budget_chars=self.config.context.min_budget_chars,
            max_budget_chars=self.config.context.max_budget_chars,
        )

    def get_status(self) -> dict[str, Any]:
        return {
            "current_summary": self._current_summary,
            "calibration_factor": self._token_estimator.get_calibration_factor(),
            "last_pressure": {
                "tier": self._last_pressure.tier.value if self._last_pressure else None,
                "ratio": self._last_pressure.pressure_ratio if self._last_pressure else None,
            },
            "last_compaction": {
                "compacted_items": (
                    self._last_compaction.compacted_items if self._last_compaction else 0
                ),
                "used_llm": self._last_compaction.used_llm if self._last_compaction else False,
            },
        }


def _split_current_request(messages: list[Message]) -> tuple[list[Message], Message | None]:
    if messages and messages[-1].role == "user":
        return messages[:-1], messages[-1]
    return messages, None


def _message_content(message: Message) -> str:
    return message.content if isinstance(message.content, str) else str(message.content)


def _messages_from_delta_items(items: list[DeltaItem]) -> list[Message]:
    return [
        Message(role=item.role, content=item.content, tool_call_id=item.tool_call_id)
        for item in items
    ]


__all__ = [
    "ContextManager",
    "ContextBuildResult",
    "Budget",
    "calculate_budget",
    "AssembledPrompt",
    "Section",
    "SectionType",
    "assemble_prompt",
    "PressureResult",
    "PressureTier",
    "calculate_pressure",
    "apply_pressure_tier",
    "apply_overflow_fallback",
    "CompactionResult",
    "DeltaItem",
    "compact_with_llm",
    "deterministic_compact",
    "extract_delta_items",
    "TokenEstimator",
    "estimate_tokens",
    "calibrate_estimation",
    "get_calibration_factor",
    "apply_tool_result_compression",
    "compress_old_tool_results",
    "offload_large_tool_results",
]
