"""Shared context variants for controlled evaluation runs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from paicli.config import PaiCliConfig
from paicli.context import ContextBuildResult, ContextManager, ContextWindowExceededError
from paicli.context.pressure import calculate_pressure_from_tokens
from paicli.llm.base import LlmClient
from paicli.prompt import PromptSections
from paicli.types import Message


@dataclass(frozen=True, slots=True)
class ContextStressProfile:
    """A named immutable per-request context budget."""

    profile_id: str
    input_budget_tokens: int
    output_reserve_tokens: int
    fingerprint: str


class FullHistoryContextManager(ContextManager):
    """Evaluation baseline that preserves history under a fixed input guard."""

    def __init__(
        self,
        *,
        config: PaiCliConfig,
        llm_client: LlmClient,
        cwd: str,
        input_budget_tokens: int,
    ) -> None:
        super().__init__(config=config, llm_client=llm_client, cwd=cwd)
        self.input_budget_tokens = input_budget_tokens

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
        del actual_usage
        sections = prompt_sections or PromptSections(
            prefix="\n\n".join(part for part in (prefix, memory) if part),
            relevant_memory=relevant_memory,
            skills=skills,
        )
        output_messages = list(messages or [])
        prepared = self.llm_client.prepare_request(
            output_messages,
            list(tools or []),
            system_prompt=sections.render(),
        ).with_quality_budget(self.input_budget_tokens, self.pressure_thresholds())
        pressure = calculate_pressure_from_tokens(
            prepared.estimated_input_tokens,
            self.input_budget_tokens,
            self.config.context,
        )
        if prepared.estimated_input_tokens > self.input_budget_tokens:
            raise ContextWindowExceededError(
                "The full-history request exceeds the evaluation input budget "
                f"({prepared.estimated_input_tokens} > {self.input_budget_tokens} input tokens)."
            )
        self._last_pressure = pressure
        return ContextBuildResult(
            system_prompt=sections.render(),
            messages=output_messages,
            prepared=prepared,
            pressure_before=pressure,
            pressure_after=pressure,
            reductions=[],
            compacted=False,
            pressure_tier=pressure.tier.value,
        )

    def quality_budget_tokens(self) -> int:
        return self.input_budget_tokens


def full_history_context_manager_factory(
    profile: ContextStressProfile,
) -> Callable[..., ContextManager]:
    """Return the production Agent construction seam for the baseline variant."""

    def factory(*, config: PaiCliConfig, llm_client: LlmClient, cwd: str) -> ContextManager:
        return FullHistoryContextManager(
            config=config,
            llm_client=llm_client,
            cwd=cwd,
            input_budget_tokens=profile.input_budget_tokens,
        )

    return factory


def load_context_stress_profile(path: str | Path) -> ContextStressProfile:
    """Load and fingerprint a strict named context-stress profile."""

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("context-stress profile must be an object")
    expected = {
        "schema_version",
        "profile_id",
        "input_budget_tokens",
        "output_reserve_tokens",
    }
    if set(data) != expected or data.get("schema_version") != 1:
        raise ValueError("invalid context-stress profile schema")
    profile_id = data.get("profile_id")
    input_budget = data.get("input_budget_tokens")
    output_reserve = data.get("output_reserve_tokens")
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ValueError("context-stress profile_id must not be empty")
    if not isinstance(input_budget, int) or isinstance(input_budget, bool) or input_budget < 1:
        raise ValueError("input_budget_tokens must be a positive integer")
    if (
        not isinstance(output_reserve, int)
        or isinstance(output_reserve, bool)
        or output_reserve < 1
    ):
        raise ValueError("output_reserve_tokens must be a positive integer")
    fingerprint = hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ContextStressProfile(
        profile_id=profile_id,
        input_budget_tokens=input_budget,
        output_reserve_tokens=output_reserve,
        fingerprint=fingerprint,
    )


__all__ = [
    "ContextStressProfile",
    "FullHistoryContextManager",
    "full_history_context_manager_factory",
    "load_context_stress_profile",
]
