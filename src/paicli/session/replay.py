from __future__ import annotations

from dataclasses import replace
from typing import Any

from paicli.session.models import MessagePart, SessionEvent, SessionMessage, SessionView


def rebuild_session_view(session_id: str, events: list[SessionEvent]) -> SessionView:
    metadata: dict[str, Any] = {}
    transcript: list[SessionMessage] = []
    model_messages: list[SessionMessage] = []
    reset_sequence: int | None = None

    for event in events:
        if event.type == "session.created":
            metadata.update(event.payload)
            continue
        if event.type == "session.metadata_updated":
            for key, value in event.payload.items():
                if value is None:
                    metadata.pop(key, None)
                else:
                    metadata[key] = value
            continue
        if event.type == "context.reset":
            model_messages.clear()
            reset_sequence = event.sequence
            continue
        if event.type == "message.hidden":
            message_id = str(event.payload.get("message_id") or "")
            transcript = [
                replace(message, hidden=True) if message.id == message_id else message
                for message in transcript
            ]
            model_messages = [message for message in model_messages if message.id != message_id]
            continue
        if not event.type.startswith("message."):
            continue
        message = message_from_event(event)
        transcript.append(message)
        if message.replayable and not message.hidden and message.status == "complete":
            model_messages.append(message)

    return SessionView(
        session_id=session_id,
        metadata=metadata,
        transcript=tuple(transcript),
        model_messages=tuple(model_messages),
        reset_sequence=reset_sequence,
    )


def message_from_event(event: SessionEvent) -> SessionMessage:
    payload = event.payload
    raw_parts = payload.get("parts")
    parts = (
        tuple(
            MessagePart(
                kind=str(part.get("kind") or "text"),
                content=str(part.get("content") or ""),
                metadata=dict(part.get("metadata") or {}),
            )
            for part in raw_parts
            if isinstance(part, dict)
        )
        if isinstance(raw_parts, list)
        else ()
    )
    return SessionMessage(
        id=str(payload["message_id"]),
        event_id=event.id,
        role=str(payload["role"]),
        parts=parts,
        created_at=event.created_at,
        status=str(payload.get("status") or "complete"),
        replayable=bool(payload.get("replayable", True)),
        interruption_reason=(
            str(payload["interruption_reason"]) if payload.get("interruption_reason") else None
        ),
    )
