from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

_CURRENT_CONTEXT_SCOPE: ContextVar[str | None] = ContextVar(
    "paicli_context_scope",
    default=None,
)


def current_context_scope() -> str | None:
    return _CURRENT_CONTEXT_SCOPE.get()


def rounded_context_percent(ratio: float) -> int:
    return math.floor(max(0.0, ratio) * 100 + 0.5)


@contextmanager
def use_context_scope(scope: str | None) -> Iterator[None]:
    token = _CURRENT_CONTEXT_SCOPE.set(scope)
    try:
        yield
    finally:
        _CURRENT_CONTEXT_SCOPE.reset(token)


@dataclass(slots=True)
class ContextUsageState:
    active: dict[str, dict[str, Any]] = field(default_factory=dict)
    retained: dict[str, Any] | None = None
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)

    def apply(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "context_usage":
            state = event.get("state")
            if state == "active":
                request_id = str(event.get("request_id") or "")
                if request_id:
                    self.active[request_id] = dict(event)
                self.pending.pop(str(event.get("scope") or ""), None)
            elif state == "retained":
                self.retained = dict(event)
                self.pending.pop(str(event.get("scope") or "agent"), None)
            elif state == "pending":
                scope = str(event.get("scope") or "agent")
                self.pending[scope] = dict(event)
        elif event_type == "context_request_finished":
            self.active.pop(str(event.get("request_id") or ""), None)
        elif event_type == "context_pending_clear":
            scope = event.get("scope")
            if scope:
                self.pending.pop(str(scope), None)
            else:
                self.pending.clear()
        elif event_type == "context_scope_clear":
            scope = str(event.get("scope") or "")
            self.active = {
                request_id: reading
                for request_id, reading in self.active.items()
                if str(reading.get("scope") or "") != scope
            }
            self.pending.pop(scope, None)

    @property
    def current(self) -> dict[str, Any] | None:
        if self.active:
            return max(
                self.active.values(),
                key=lambda item: int(item.get("used_tokens") or 0),
            )
        if self.pending:
            return max(
                self.pending.values(),
                key=lambda item: int(item.get("used_tokens") or 0),
            )
        return self.retained

    @property
    def active_count(self) -> int:
        return len(self.active)
