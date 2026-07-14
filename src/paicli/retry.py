from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

TRANSIENT_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    enabled: bool = True
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 8.0
    max_retry_after: float = 30.0

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if self.base_delay < 0 or self.max_delay < 0 or self.max_retry_after < 0:
            raise ValueError("retry delays must be non-negative")


@dataclass(frozen=True, slots=True)
class RetryPolicyOverride:
    enabled: bool | None = None
    max_retries: int | None = None
    base_delay: float | None = None
    max_delay: float | None = None
    max_retry_after: float | None = None

    def apply(self, base: RetryPolicy) -> RetryPolicy:
        return RetryPolicy(
            enabled=base.enabled if self.enabled is None else self.enabled,
            max_retries=base.max_retries if self.max_retries is None else self.max_retries,
            base_delay=base.base_delay if self.base_delay is None else self.base_delay,
            max_delay=base.max_delay if self.max_delay is None else self.max_delay,
            max_retry_after=(
                base.max_retry_after if self.max_retry_after is None else self.max_retry_after
            ),
        )


@dataclass(frozen=True, slots=True)
class RetryDecision:
    retryable: bool
    error_kind: str
    retry_after: float | None = None


def compute_retry_delay(
    policy: RetryPolicy,
    *,
    attempt: int,
    retry_after: float | None,
) -> float:
    """Return full-jitter backoff for a zero-based failed attempt."""
    if retry_after is not None:
        return min(retry_after, policy.max_retry_after)
    cap = min(policy.max_delay, policy.base_delay * (2**attempt))
    return random.uniform(0.0, cap)


def classify_transient_error(exc: Exception) -> RetryDecision:
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return RetryDecision(True, "timeout")
    if isinstance(exc, (httpx.NetworkError, ConnectionError)):
        return RetryDecision(True, "connection")
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        status = response.status_code
        retry_after = parse_retry_after(response.headers.get("retry-after"))
        if status == 429:
            return RetryDecision(True, "rate_limit", retry_after)
        if status in TRANSIENT_HTTP_STATUSES:
            kind = "overloaded" if status == 503 else "server_error"
            return RetryDecision(True, kind, retry_after)
        if _response_reports_overload(response):
            return RetryDecision(True, "overloaded", retry_after)
        return RetryDecision(False, "http_error")
    return RetryDecision(False, "unknown")


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max((parsed - datetime.now(UTC)).total_seconds(), 0.0)
    except (TypeError, ValueError, OverflowError):
        return None


def _response_reports_overload(response: httpx.Response) -> bool:
    try:
        body: Any = response.json()
    except (ValueError, RuntimeError):
        body = response.text
    normalized = str(body).lower()
    return "overloaded" in normalized or "overload" in normalized
