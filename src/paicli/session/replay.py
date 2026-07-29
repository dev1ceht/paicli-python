from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from paicli.session.models import MessagePart, SessionEvent, SessionMessage, SessionView


def rebuild_session_view(
    session_id: str,
    events: list[SessionEvent],
    *,
    blob_loader: Callable[[str], bytes] | None = None,
) -> SessionView:
    metadata: dict[str, Any] = {}
    session_history: list[SessionMessage] = []
    model_messages: list[SessionMessage] = []
    reset_sequence: int | None = None
    last_compaction: dict[str, Any] | None = None
    context_checkpoint: dict[str, Any] | None = None
    context_checkpoint_sequence: int | None = None

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
            last_compaction = None
            context_checkpoint = None
            context_checkpoint_sequence = None
            continue
        if event.type == "context.compacted":
            last_compaction = dict(event.payload)
            continue
        if event.type == "context.checkpoint_created":
            checkpoint_messages = event.payload.get("messages")
            messages_content_hash = event.payload.get("messages_content_hash")
            if messages_content_hash:
                if blob_loader is None:
                    raise ValueError("context checkpoint blob requires a blob loader")
                checkpoint_messages = json.loads(
                    blob_loader(str(messages_content_hash)).decode("utf-8")
                )
            if not isinstance(checkpoint_messages, list):
                raise TypeError("context checkpoint messages must be a JSON array")
            context_checkpoint = {
                **event.payload,
                "messages": checkpoint_messages,
                "summary": (
                    str(last_compaction.get("summary") or "") if last_compaction else ""
                ),
                "compaction": (
                    dict(last_compaction.get("compaction") or {}) if last_compaction else {}
                ),
                "pressure": (
                    dict(last_compaction.get("pressure") or {}) if last_compaction else {}
                ),
                "provider": (
                    str(last_compaction.get("provider") or "") if last_compaction else ""
                ),
                "model": str(last_compaction.get("model") or "") if last_compaction else "",
            }
            context_checkpoint_sequence = event.sequence
            continue
        if event.type == "message.hidden":
            message_id = str(event.payload.get("message_id") or "")
            session_history = [
                replace(message, hidden=True) if message.id == message_id else message
                for message in session_history
            ]
            model_messages = [message for message in model_messages if message.id != message_id]
            continue
        if not event.type.startswith("message."):
            continue
        message = message_from_event(event, blob_loader=blob_loader)
        session_history.append(message)
        if message.replayable and not message.hidden and message.status == "complete":
            model_messages.append(message)

    return SessionView(
        session_id=session_id,
        metadata=metadata,
        session_history=tuple(session_history),
        model_messages=tuple(model_messages),
        reset_sequence=reset_sequence,
        context_checkpoint=context_checkpoint,
        context_checkpoint_sequence=context_checkpoint_sequence,
    )


def message_from_event(
    event: SessionEvent,
    *,
    blob_loader: Callable[[str], bytes] | None = None,
) -> SessionMessage:
    payload = event.payload
    message_id = payload.get("message_id")
    role = payload.get("role")
    if not isinstance(message_id, str) or not message_id:
        raise TypeError("message_id must be a non-empty string")
    if role not in {"user", "assistant", "tool"}:
        raise ValueError(f"unsupported message role: {role}")
    raw_parts = payload.get("parts")
    if not isinstance(raw_parts, list) or not raw_parts:
        raise TypeError("message parts must be a non-empty JSON array")
    parts: list[MessagePart] = []
    for part in raw_parts:
        if not isinstance(part, dict):
            raise TypeError("message part must be a JSON object")
        raw_metadata = part.get("metadata", {})
        if not isinstance(raw_metadata, dict):
            raise TypeError("message part metadata must be a JSON object")
        metadata = dict(raw_metadata)
        raw_content = part.get("content", "")
        if not isinstance(raw_content, str):
            raise TypeError("message part content must be a string")
        content = raw_content
        content_hash = metadata.get("content_hash")
        if not content and content_hash and blob_loader is not None:
            content = blob_loader(str(content_hash)).decode("utf-8")
        parts.append(
            MessagePart(
                kind=str(part.get("kind") or "text"),
                content=content,
                metadata=metadata,
            )
        )
    return SessionMessage(
        id=message_id,
        event_id=event.id,
        role=role,
        parts=tuple(parts),
        created_at=event.created_at,
        status=str(payload.get("status") or "complete"),
        replayable=bool(payload.get("replayable", True)),
        interruption_reason=(
            str(payload["interruption_reason"]) if payload.get("interruption_reason") else None
        ),
    )
