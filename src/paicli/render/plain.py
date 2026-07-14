from __future__ import annotations

import sys
from typing import Any


class PlainRenderer:
    def __init__(self, *, print_events: bool = True):
        self.print_events = print_events

    def handle(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "text_delta":
            sys.stdout.write(str(event.get("text") or ""))
            sys.stdout.flush()
        elif self.print_events and event_type == "tool_call":
            sys.stdout.write(f"\n[tool] {event.get('name')} {event.get('input')}\n")
            sys.stdout.flush()
        elif self.print_events and event_type == "tool_result":
            marker = "error" if event.get("is_error") else "result"
            sys.stdout.write(f"[tool:{marker}] {event.get('name')}: {event.get('result')}\n")
            sys.stdout.flush()
        elif self.print_events and event_type == "retry":
            sys.stdout.write(
                f"[retry] {event.get('scope')} attempt={event.get('attempt')} "
                f"delay={event.get('delay')} error={event.get('error_kind')}\n"
            )
            sys.stdout.flush()
        elif self.print_events and event_type == "retry_exhausted":
            sys.stdout.write(
                f"[retry-exhausted] {event.get('scope')} "
                f"attempts={event.get('attempt')} error={event.get('error_kind')}\n"
            )
            sys.stdout.flush()
        elif self.print_events and event_type == "plan_aggregate_result":
            sys.stdout.write(
                f"[plan-summary] status={event.get('status')} "
                f"attempts={event.get('attempts')} "
                f"completed={len(event.get('completed') or {})} "
                f"failed={len(event.get('failed') or {})}\n"
            )
            sys.stdout.flush()

    def newline(self) -> None:
        sys.stdout.write("\n")
        sys.stdout.flush()
