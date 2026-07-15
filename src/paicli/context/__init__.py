from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

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
    message_content_for_compaction,
)
from paicli.context.pressure import (
    PressureResult,
    PressureTier,
    apply_pressure_tier,
    calculate_pressure,
    calculate_pressure_from_tokens,
)
from paicli.context.telemetry import use_context_scope
from paicli.context.token_estimator import (
    TokenEstimator,
    calibrate_estimation,
    estimate_tokens,
    get_calibration_factor,
)
from paicli.context.tool_result import (
    apply_tool_result_compression,
    cleanup_session_tool_results,
    cleanup_stale_tool_results,
    compress_next_old_tool_result,
    compress_old_tool_results,
    offload_large_tool_results,
    offload_next_tool_result,
)
from paicli.llm.base import LlmClient, PreparedOutboundRequest
from paicli.prompt import PromptSections
from paicli.types import Message


class ContextWindowExceededError(RuntimeError):
    """The final protected outbound request cannot fit the physical model window."""


_SUMMARY_PREFIX = "[Previous conversation summary]\n"


@dataclass
class ContextBuildResult:
    system_prompt: str
    messages: list[Message]
    prepared: PreparedOutboundRequest | None = None
    pressure_before: PressureResult | None = None
    pressure_after: PressureResult | None = None
    reductions: list[str] = field(default_factory=list)
    compacted: bool = False
    pressure_tier: str | None = None


@dataclass
class ContextManager:
    """Build prompts and keep the actual message list under control."""

    config: PaiCliConfig
    llm_client: LlmClient
    cwd: str
    session_id: str = field(default_factory=lambda: uuid4().hex)

    _current_summary: str = ""
    _token_estimator: TokenEstimator = field(default_factory=TokenEstimator)
    _last_pressure: PressureResult | None = None
    _last_compaction: CompactionResult | None = None

    def __post_init__(self) -> None:
        cleanup_stale_tool_results(
            self.config.context.tool_result_storage_dir,
            max_age_days=7,
        )

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
        """Build role-stable system instructions without serializing conversation messages."""
        del history, current_request, actual_usage
        return "\n\n".join(
            section.strip()
            for section in [prefix, memory, skills, relevant_memory]
            if section.strip()
        )

    async def build_turn_context(
        self,
        *,
        prefix: str = "",
        memory: str = "",
        skills: str = "",
        relevant_memory: str = "",
        messages: list[Message] | None = None,
        prompt_sections: PromptSections | None = None,
        tools: list[dict[str, Any]] | None = None,
        actual_usage: dict[str, int] | None = None,
    ) -> ContextBuildResult:
        all_messages = list(messages or [])
        del actual_usage
        sections = prompt_sections or PromptSections(
            prefix="\n\n".join(part.strip() for part in (prefix, memory) if part.strip()),
            relevant_memory=relevant_memory,
            skills=skills,
        )
        budget = self._calculate_budget()
        tool_definitions = list(tools or [])
        output_messages = all_messages
        prepared, pressure = self._prepare_candidate(
            sections,
            output_messages,
            tool_definitions,
            budget,
        )
        pressure_before = pressure
        start_tier = pressure.tier
        reductions: list[str] = []

        if start_tier != PressureTier.TIER0_OBSERVE:
            while not self._exited_start_tier(pressure, start_tier):
                output_messages, changed = offload_next_tool_result(
                    output_messages,
                    max_total_bytes=self.config.context.tool_result_max_total_bytes,
                    preview_chars=self.config.context.tool_result_preview_chars,
                    storage_dir=self.config.context.tool_result_storage_dir,
                    session_id=self.session_id,
                )
                if not changed:
                    break
                _append_reduction(reductions, "tool_offload")
                prepared, pressure = self._prepare_candidate(
                    sections, output_messages, tool_definitions, budget
                )

            while not self._exited_start_tier(pressure, start_tier):
                output_messages, changed = compress_next_old_tool_result(
                    output_messages,
                    keep_recent=self.config.context.tool_result_keep_recent,
                )
                if not changed:
                    break
                _append_reduction(reductions, "old_tool_result")
                prepared, pressure = self._prepare_candidate(
                    sections, output_messages, tool_definitions, budget
                )

        if start_tier in {PressureTier.TIER2_PRUNE, PressureTier.TIER3_SUMMARY}:
            while (
                not self._exited_start_tier(pressure, start_tier)
                and sections.relevant_memory
            ):
                reduced = sections.drop_least_relevant_memory()
                if reduced == sections:
                    break
                sections = reduced
                _append_reduction(reductions, "relevant_memory")
                prepared, pressure = self._prepare_candidate(
                    sections, output_messages, tool_definitions, budget
                )
            if not self._exited_start_tier(pressure, start_tier) and sections.skills:
                sections = sections.without_skills()
                _append_reduction(reductions, "skills")
                prepared, pressure = self._prepare_candidate(
                    sections, output_messages, tool_definitions, budget
                )

        physical_limit = max(
            0,
            self.llm_client.max_context_window
            - self.config.context.output_reserve_tokens,
        )
        while prepared.estimated_input_tokens > physical_limit:
            output_messages, changed = offload_next_tool_result(
                output_messages,
                max_total_bytes=self.config.context.tool_result_max_total_bytes,
                preview_chars=self.config.context.tool_result_preview_chars,
                storage_dir=self.config.context.tool_result_storage_dir,
                session_id=self.session_id,
                force=True,
            )
            if not changed:
                break
            _append_reduction(reductions, "tool_offload_overflow")
            prepared, pressure = self._prepare_candidate(
                sections, output_messages, tool_definitions, budget
            )

        compacted = False
        if (
            start_tier == PressureTier.TIER3_SUMMARY
            and not self._exited_start_tier(pressure, start_tier)
            and self.config.features.context_compression
        ):
            compacted_result = await self._compact_structured_history(
                sections=sections,
                messages=output_messages,
                tools=tool_definitions,
                budget=budget,
                current_prepared=prepared,
            )
            if compacted_result is not None:
                output_messages, prepared, pressure, actions = compacted_result
                compacted = True
                for action in actions:
                    _append_reduction(reductions, action)

        if prepared.estimated_input_tokens > physical_limit:
            raise ContextWindowExceededError(
                "The protected request is too large for the model context window "
                f"({prepared.estimated_input_tokens} > {physical_limit} input tokens)."
            )

        self._last_pressure = pressure
        return ContextBuildResult(
            system_prompt=sections.render(),
            messages=output_messages,
            prepared=prepared,
            pressure_before=pressure_before,
            pressure_after=pressure,
            reductions=reductions,
            compacted=compacted,
            pressure_tier=pressure.tier.value,
        )

    def _calculate_budget(self) -> Budget:
        return calculate_budget(
            context_window=self.llm_client.max_context_window,
            utilization_rate=self.config.context.utilization_rate,
            output_reserve_tokens=self.config.context.output_reserve_tokens,
            min_budget_chars=self.config.context.min_budget_chars,
            max_budget_chars=self.config.context.max_budget_chars,
        )

    def _prepare_candidate(
        self,
        sections: PromptSections,
        messages: list[Message],
        tools: list[dict[str, Any]],
        budget: Budget,
    ) -> tuple[PreparedOutboundRequest, PressureResult]:
        prepare_request = getattr(self.llm_client, "prepare_request", None)
        if callable(prepare_request):
            prepared = prepare_request(
                messages,
                tools,
                system_prompt=sections.render(),
            )
        else:
            prepared = _prepare_compatibility_request(
                messages,
                tools,
                system_prompt=sections.render(),
            )
        prepared = prepared.with_quality_budget(
            budget.prompt_tokens,
            (
                self.config.context.tier1_threshold,
                self.config.context.tier2_threshold,
                self.config.context.tier3_threshold,
            ),
        )
        pressure = calculate_pressure_from_tokens(
            prepared.estimated_input_tokens,
            budget.prompt_tokens,
            self.config.context,
        )
        return prepared, pressure

    def _exited_start_tier(
        self,
        pressure: PressureResult,
        start_tier: PressureTier,
    ) -> bool:
        thresholds = {
            PressureTier.TIER0_OBSERVE: 0.0,
            PressureTier.TIER1_SNIP: self.config.context.tier1_threshold,
            PressureTier.TIER2_PRUNE: self.config.context.tier2_threshold,
            PressureTier.TIER3_SUMMARY: self.config.context.tier3_threshold,
        }
        return pressure.pressure_ratio < thresholds[start_tier]

    async def _compact_structured_history(
        self,
        *,
        sections: PromptSections,
        messages: list[Message],
        tools: list[dict[str, Any]],
        budget: Budget,
        current_prepared: PreparedOutboundRequest,
    ) -> tuple[list[Message], PreparedOutboundRequest, PressureResult, list[str]] | None:
        history, current = _split_current_request(messages)
        prior_summary = self._current_summary
        if history and _is_summary_message(history[0]):
            if not prior_summary:
                prior_summary = str(history[0].content)[len(_SUMMARY_PREFIX) :]
            history = history[1:]
        delta_messages, protected_messages = _partition_history(
            history,
            protected_turns=self.config.context.protected_turns,
        )
        delta_items = _delta_items(delta_messages)
        if not delta_items:
            return None

        compaction = await self._create_compaction(delta_items, prior_summary)

        candidate_messages = _summary_messages(
            compaction.summary,
            protected_messages,
            current,
        )
        candidate_prepared, candidate_pressure = self._prepare_candidate(
            sections,
            candidate_messages,
            tools,
            budget,
        )
        if (
            compaction.used_llm
            and candidate_prepared.estimated_input_tokens
            >= current_prepared.estimated_input_tokens
        ):
            compaction = deterministic_compact(
                delta_items,
                prior_summary=prior_summary,
                llm_usage=compaction.llm_usage,
            )
            candidate_messages = _summary_messages(
                compaction.summary,
                protected_messages,
                current,
            )
            candidate_prepared, candidate_pressure = self._prepare_candidate(
                sections,
                candidate_messages,
                tools,
                budget,
            )

        if (
            candidate_prepared.estimated_input_tokens
            >= current_prepared.estimated_input_tokens
        ):
            return None

        actions = ["history_summary" if compaction.used_llm else "history_deterministic"]
        retained_summary = compaction.summary
        physical_limit = max(
            0,
            self.llm_client.max_context_window
            - self.config.context.output_reserve_tokens,
        )
        if (
            candidate_pressure.pressure_ratio >= self.config.context.tier3_threshold
            or candidate_prepared.estimated_input_tokens > physical_limit
        ):
            turns = _history_turns(history)
            if len(turns) >= 2:
                emergency = deterministic_compact(_delta_items(turns[-2]))
                combined_summary = (
                    f"{compaction.summary}\n\n---\n"
                    f"Emergency delta from the second-latest turn:\n{emergency.summary}"
                )
                aggressive_messages = _summary_messages(
                    combined_summary,
                    turns[-1],
                    current,
                )
                aggressive_prepared, aggressive_pressure = self._prepare_candidate(
                    sections,
                    aggressive_messages,
                    tools,
                    budget,
                )
                if (
                    aggressive_prepared.estimated_input_tokens
                    < candidate_prepared.estimated_input_tokens
                ):
                    candidate_messages = aggressive_messages
                    candidate_prepared = aggressive_prepared
                    candidate_pressure = aggressive_pressure
                    retained_summary = combined_summary
                    actions.append("history_aggressive")

        compaction.protected_items = len(protected_messages)
        self._last_compaction = compaction
        self._current_summary = retained_summary
        return candidate_messages, candidate_prepared, candidate_pressure, actions

    async def _create_compaction(
        self,
        delta_items: list[DeltaItem],
        prior_summary: str,
    ) -> CompactionResult:
        """Generate a summary; benchmark variants override only this strategy seam."""
        try:
            with use_context_scope(None):
                return await compact_with_llm(
                    delta_items,
                    self.llm_client,
                    prior_summary=prior_summary,
                )
        except Exception:
            return deterministic_compact(
                delta_items,
                prior_summary=prior_summary,
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

    def quality_budget_tokens(self) -> int:
        return self._calculate_budget().prompt_tokens

    def checkpoint_state(
        self,
    ) -> tuple[str, PressureResult | None, CompactionResult | None]:
        """Capture state that must advance atomically with retained history."""
        return self._current_summary, self._last_pressure, self._last_compaction

    def restore_state(
        self,
        checkpoint: tuple[str, PressureResult | None, CompactionResult | None],
    ) -> None:
        self._current_summary, self._last_pressure, self._last_compaction = checkpoint

    def pressure_thresholds(self) -> tuple[float, float, float]:
        return (
            self.config.context.tier1_threshold,
            self.config.context.tier2_threshold,
            self.config.context.tier3_threshold,
        )

    def reset(self) -> None:
        cleanup_session_tool_results(
            self.config.context.tool_result_storage_dir,
            self.session_id,
        )
        self.session_id = uuid4().hex
        self._current_summary = ""
        self._last_pressure = None
        self._last_compaction = None
        self._token_estimator.reset_calibration()

    def close(self) -> None:
        cleanup_session_tool_results(
            self.config.context.tool_result_storage_dir,
            self.session_id,
        )


def _split_current_request(messages: list[Message]) -> tuple[list[Message], Message | None]:
    if messages and messages[-1].role == "user":
        return messages[:-1], messages[-1]
    return messages, None


def _message_content(message: Message) -> str:
    return message.content if isinstance(message.content, str) else str(message.content)


def _prepare_compatibility_request(
    messages: list[Message],
    tools: list[dict[str, Any]],
    *,
    system_prompt: str,
) -> PreparedOutboundRequest:
    """Freeze a generic request for older custom clients lacking provider preparation."""
    payload = {
        "system_prompt": system_prompt,
        "messages": [
            {
                "role": message.role,
                "content": message.content,
                "name": message.name,
                "tool_call_id": message.tool_call_id,
                "tool_calls": message.tool_calls,
                "reasoning_content": message.reasoning_content,
            }
            for message in messages
        ],
        "tools": tools,
    }
    payload_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return PreparedOutboundRequest(
        payload_json=payload_json,
        estimated_input_tokens=estimate_tokens(payload_json.decode("utf-8")),
    )


def _messages_from_delta_items(items: list[DeltaItem]) -> list[Message]:
    return [
        Message(role=item.role, content=item.content, tool_call_id=item.tool_call_id)
        for item in items
    ]


def _partition_history(
    messages: list[Message],
    *,
    protected_turns: int,
) -> tuple[list[Message], list[Message]]:
    turns = _history_turns(messages)
    if len(turns) <= protected_turns:
        return [], [message for turn in turns for message in turn]
    split_at = len(turns) - protected_turns
    return (
        [message for turn in turns[:split_at] for message in turn],
        [message for turn in turns[split_at:] for message in turn],
    )


def _history_turns(messages: list[Message]) -> list[list[Message]]:
    turns: list[list[Message]] = []
    current_turn: list[Message] = []
    for message in messages:
        if message.role == "user" and current_turn:
            turns.append(current_turn)
            current_turn = []
        current_turn.append(message)
    if current_turn:
        turns.append(current_turn)
    return turns


def _delta_items(messages: list[Message]) -> list[DeltaItem]:
    return [
        DeltaItem(
            turn_id=index,
            role=message.role,
            content=message_content_for_compaction(message),
            tool_call_id=message.tool_call_id,
        )
        for index, message in enumerate(messages)
    ]


def _summary_messages(
    summary: str,
    protected_messages: list[Message],
    current: Message | None,
) -> list[Message]:
    return [
        Message(role="system", content=f"{_SUMMARY_PREFIX}{summary}"),
        *protected_messages,
        *([current] if current else []),
    ]


def _is_summary_message(message: Message) -> bool:
    return message.role == "system" and str(message.content).startswith(_SUMMARY_PREFIX)


def _append_reduction(reductions: list[str], name: str) -> None:
    if name not in reductions:
        reductions.append(name)


__all__ = [
    "ContextManager",
    "ContextBuildResult",
    "ContextWindowExceededError",
    "Budget",
    "calculate_budget",
    "AssembledPrompt",
    "Section",
    "SectionType",
    "assemble_prompt",
    "PressureResult",
    "PressureTier",
    "calculate_pressure",
    "calculate_pressure_from_tokens",
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
