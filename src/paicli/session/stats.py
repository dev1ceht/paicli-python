from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from paicli.session.models import SessionEvent
from paicli.usage import TokenUsage, UsageRecord


@dataclass(frozen=True, slots=True)
class CostTotal:
    currency: str
    source: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class SessionStats:
    tokens: TokenUsage
    actual_tokens: TokenUsage
    estimated_tokens: TokenUsage
    inherited_tokens: TokenUsage
    costs: tuple[CostTotal, ...]
    message_count: int
    user_turn_count: int
    tool_call_count: int
    tool_result_count: int
    coverage: Literal["complete", "partial"]
    cache_hit_rate: float | None

    @property
    def total_cost(self) -> Decimal:
        currencies = {cost.currency for cost in self.costs}
        if len(currencies) > 1:
            raise ValueError("total cost is undefined across multiple currencies")
        return sum((cost.amount for cost in self.costs), start=Decimal("0"))

    def cost_in(self, currency: str) -> Decimal:
        return sum(
            (cost.amount for cost in self.costs if cost.currency == currency),
            start=Decimal("0"),
        )


def calculate_session_stats(events: Iterable[SessionEvent]) -> SessionStats:
    actual = TokenUsage()
    estimated = TokenUsage()
    inherited = TokenUsage()
    cost_totals: dict[tuple[str, str], Decimal] = {}
    message_count = 0
    user_turn_count = 0
    tool_call_count = 0
    tool_result_count = 0
    billable_turns: set[tuple[str, str]] = set()
    usage_turns: set[tuple[str, str]] = set()

    for event in events:
        if event.type.startswith("message.") and event.type != "message.hidden":
            message_count += 1
            role = str(event.payload.get("role") or "")
            if role == "user":
                user_turn_count += 1
            elif role == "assistant":
                billable_turns.add(_event_turn_key(event))
                parts = event.payload.get("parts") or []
                tool_call_count += sum(
                    1
                    for part in parts
                    if isinstance(part, dict) and part.get("kind") == "tool_call"
                )
            elif role == "tool":
                tool_result_count += 1
            continue
        if event.type == "context.compacted":
            compaction = event.payload.get("compaction")
            if isinstance(compaction, dict) and compaction.get("used_llm"):
                billable_turns.add(_event_turn_key(event))
            continue
        if event.type != "usage.recorded":
            continue

        record = UsageRecord.from_payload(event.payload)
        usage_turns.add(_event_turn_key(event))
        if event.source_session_id is not None:
            inherited = inherited + record.tokens
            continue
        if record.usage_source == "actual":
            actual = actual + record.tokens
        else:
            estimated = estimated + record.tokens
        if record.cost is not None:
            key = (record.cost.currency, record.cost.source)
            cost_totals[key] = cost_totals.get(key, Decimal("0")) + record.cost.amount

    costs = tuple(
        CostTotal(currency=currency, source=source, amount=amount)
        for (currency, source), amount in sorted(cost_totals.items())
    )
    cache_hit_rate = (
        actual.cache_read_tokens / actual.prompt_tokens if actual.prompt_tokens > 0 else None
    )
    return SessionStats(
        tokens=actual + estimated,
        actual_tokens=actual,
        estimated_tokens=estimated,
        inherited_tokens=inherited,
        costs=costs,
        message_count=message_count,
        user_turn_count=user_turn_count,
        tool_call_count=tool_call_count,
        tool_result_count=tool_result_count,
        coverage="partial" if billable_turns - usage_turns else "complete",
        cache_hit_rate=cache_hit_rate,
    )


def _event_turn_key(event: SessionEvent) -> tuple[str, str]:
    return (
        event.source_session_id or event.session_id,
        event.turn_id or event.id,
    )
