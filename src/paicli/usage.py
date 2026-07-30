from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, cast

UsagePurpose = Literal["assistant", "context_summary", "tool", "branch_summary"]
UsageSource = Literal["actual", "estimated"]
CostSource = Literal["provider", "catalog_estimate", "unknown"]


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __post_init__(self) -> None:
        values = (
            self.input_tokens,
            self.output_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values
        ):
            raise ValueError("token usage values must be non-negative integers")

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens

    def __add__(self, other: object) -> TokenUsage:
        if not isinstance(other, TokenUsage):
            return NotImplemented
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )

    def to_payload(self) -> dict[str, int]:
        return {
            "input": self.input_tokens,
            "output": self.output_tokens,
            "cache_read": self.cache_read_tokens,
            "cache_write": self.cache_write_tokens,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TokenUsage:
        return cls(
            input_tokens=_non_negative_int(payload, "input"),
            output_tokens=_non_negative_int(payload, "output"),
            cache_read_tokens=_non_negative_int(payload, "cache_read"),
            cache_write_tokens=_non_negative_int(payload, "cache_write"),
        )


@dataclass(frozen=True, slots=True)
class UsageCost:
    amount: Decimal
    currency: str
    source: CostSource

    def __post_init__(self) -> None:
        if not self.amount.is_finite() or self.amount < 0:
            raise ValueError("usage cost must be a finite non-negative decimal")
        if not self.currency:
            raise ValueError("usage cost currency must not be empty")
        if self.source not in {"provider", "catalog_estimate", "unknown"}:
            raise ValueError(f"unsupported usage cost source: {self.source}")

    def to_payload(self) -> dict[str, str]:
        return {
            "amount": str(self.amount),
            "currency": self.currency,
            "source": self.source,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> UsageCost:
        try:
            amount = Decimal(str(payload.get("amount", "")))
        except InvalidOperation as exc:
            raise ValueError("usage cost amount must be decimal") from exc
        source = str(payload.get("source") or "")
        if source not in {"provider", "catalog_estimate", "unknown"}:
            raise ValueError(f"unsupported usage cost source: {source}")
        return cls(
            amount=amount,
            currency=str(payload.get("currency") or ""),
            source=cast(CostSource, source),
        )


@dataclass(frozen=True, slots=True)
class UsageRecord:
    usage_id: str
    request_id: str | None
    provider: str
    model: str
    purpose: UsagePurpose
    tokens: TokenUsage
    cost: UsageCost | None
    usage_source: UsageSource

    def __post_init__(self) -> None:
        if not self.usage_id:
            raise ValueError("usage id must not be empty")
        if not self.provider:
            raise ValueError("usage provider must not be empty")
        if not self.model:
            raise ValueError("usage model must not be empty")
        if self.purpose not in {"assistant", "context_summary", "tool", "branch_summary"}:
            raise ValueError(f"unsupported usage purpose: {self.purpose}")
        if self.usage_source not in {"actual", "estimated"}:
            raise ValueError(f"unsupported usage source: {self.usage_source}")

    def to_payload(self) -> dict[str, Any]:
        return {
            "usage_id": self.usage_id,
            "request_id": self.request_id,
            "provider": self.provider,
            "model": self.model,
            "purpose": self.purpose,
            "usage_source": self.usage_source,
            "tokens": self.tokens.to_payload(),
            "cost": self.cost.to_payload() if self.cost else None,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> UsageRecord:
        tokens = payload.get("tokens")
        if not isinstance(tokens, dict):
            raise TypeError("usage tokens must be an object")
        raw_cost = payload.get("cost")
        if raw_cost is not None and not isinstance(raw_cost, dict):
            raise TypeError("usage cost must be an object or null")
        purpose = str(payload.get("purpose") or "")
        usage_source = str(payload.get("usage_source") or "")
        return cls(
            usage_id=str(payload.get("usage_id") or ""),
            request_id=(
                str(payload["request_id"]) if payload.get("request_id") is not None else None
            ),
            provider=str(payload.get("provider") or ""),
            model=str(payload.get("model") or ""),
            purpose=cast(UsagePurpose, purpose),
            tokens=TokenUsage.from_payload(tokens),
            cost=UsageCost.from_payload(raw_cost) if raw_cost is not None else None,
            usage_source=cast(UsageSource, usage_source),
        )


def _non_negative_int(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field, 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TypeError(f"{field} must be a non-negative integer")
    return value
