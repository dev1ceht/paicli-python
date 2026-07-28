from __future__ import annotations

from typing import Any


def validate_event_payload(event_type: str, payload: dict[str, Any]) -> None:
    """Validate event-specific invariants at write and replay boundaries."""

    if event_type == "message.hidden":
        _require_non_empty_string(payload, "message_id")
        return
    if event_type == "session.metadata_updated":
        title = payload.get("title")
        if title is not None and (not isinstance(title, str) or not title):
            raise TypeError("session title must be a non-empty string")
        return
    if not event_type.startswith("message."):
        return

    _require_non_empty_string(payload, "message_id")
    role = _require_non_empty_string(payload, "role")
    expected_role = (
        "assistant"
        if event_type == "message.assistant.partial"
        else event_type.removeprefix("message.")
    )
    if role != expected_role or role not in {"user", "assistant", "tool"}:
        raise ValueError(f"message role {role!r} does not match event type {event_type!r}")
    parts = payload.get("parts")
    if not isinstance(parts, list) or not parts:
        raise TypeError("message parts must be a non-empty JSON array")
    for part in parts:
        if not isinstance(part, dict):
            raise TypeError("message part must be a JSON object")
        if not isinstance(part.get("content", ""), str):
            raise TypeError("message part content must be a string")
        if not isinstance(part.get("metadata", {}), dict):
            raise TypeError("message part metadata must be a JSON object")

    status = payload.get("status", "complete")
    replayable = payload.get("replayable", True)
    if not isinstance(replayable, bool):
        raise TypeError("message replayable must be a boolean")
    if event_type == "message.assistant.partial":
        if status != "partial" or replayable:
            raise ValueError("partial assistant messages must be partial and non-replayable")
    elif status != "complete":
        raise ValueError("complete message events must have complete status")


def _require_non_empty_string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise TypeError(f"{field} must be a non-empty string")
    return value
