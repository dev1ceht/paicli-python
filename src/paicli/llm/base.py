from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any, Protocol

from paicli.types import Message


@dataclass(frozen=True, slots=True)
class PreparedOutboundRequest:
    """Frozen provider request used for both measurement and transmission."""

    payload_json: bytes
    estimated_input_tokens: int
    quality_budget_tokens: int | None = None
    pressure_thresholds: tuple[float, float, float] = (0.50, 0.70, 0.90)

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json)

    def with_quality_budget(
        self,
        tokens: int,
        thresholds: tuple[float, float, float] | None = None,
    ) -> PreparedOutboundRequest:
        values = thresholds if thresholds is not None else self.pressure_thresholds
        return replace(
            self,
            quality_budget_tokens=max(0, tokens),
            pressure_thresholds=values,
        )


class LlmClient(Protocol):
    model_name: str
    provider_name: str
    max_context_window: int

    def prepare_request(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        system_prompt: str,
    ) -> PreparedOutboundRequest: ...

    def send_prepared(
        self,
        request: PreparedOutboundRequest,
    ) -> AsyncIterator[dict[str, Any]]: ...

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        system_prompt: str,
    ) -> AsyncIterator[dict[str, Any]]: ...
