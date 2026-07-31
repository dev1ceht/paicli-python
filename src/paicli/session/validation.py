from __future__ import annotations

from typing import Any


def validate_event_payload(event_type: str, payload: dict[str, Any]) -> None:
    """Validate event-specific invariants at write and replay boundaries."""

    if event_type == "usage.recorded":
        from paicli.usage import UsageRecord

        UsageRecord.from_payload(payload)
        return
    if event_type == "message.hidden":
        _require_non_empty_string(payload, "message_id")
        return
    if event_type == "session.metadata_updated":
        title = payload.get("title")
        if title is not None and (not isinstance(title, str) or not title):
            raise TypeError("session title must be a non-empty string")
        return
    if event_type == "context.compacted":
        _require_non_empty_string(payload, "checkpoint_id")
        _require_non_empty_string(payload, "summary")
        if not isinstance(payload.get("compaction"), dict):
            raise TypeError("context compaction must be a JSON object")
        if not isinstance(payload.get("pressure", {}), dict):
            raise TypeError("context pressure must be a JSON object")
        return
    if event_type == "context.checkpoint_created":
        _require_non_empty_string(payload, "checkpoint_id")
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise TypeError("context checkpoint messages must be a JSON array")
        for message in messages:
            if not isinstance(message, dict):
                raise TypeError("context checkpoint message must be a JSON object")
            if message.get("role") not in {"system", "user", "assistant", "tool"}:
                raise ValueError("context checkpoint message has an invalid role")
            if not isinstance(message.get("content"), (str, list)):
                raise TypeError("context checkpoint message content must be text or a list")
            if message.get("source_message_id") is not None:
                _require_non_empty_string(message, "source_message_id")
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
        metadata = part.get("metadata", {})
        if not isinstance(metadata, dict):
            raise TypeError("message part metadata must be a JSON object")
        kind = str(part.get("kind") or "text")
        if kind == "tool_call":
            _require_non_empty_string(metadata, "tool_call_id")
            _require_non_empty_string(metadata, "tool_name")
            if not isinstance(metadata.get("arguments"), dict):
                raise TypeError("tool call arguments must be a JSON object")
            if not isinstance(metadata.get("raw_call"), dict):
                raise TypeError("raw tool call must be a JSON object")
        elif kind == "tool_result":
            _require_non_empty_string(metadata, "tool_call_id")
            _require_non_empty_string(metadata, "tool_name")
            if not isinstance(metadata.get("is_error"), bool):
                raise TypeError("tool result is_error must be a boolean")

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
