from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class BlobReference:
    content_hash: str
    role: str


@dataclass(frozen=True, slots=True)
class StoredBlob:
    content_hash: str
    content_type: str
    compression: str
    original_size: int
    stored_size: int
    data: bytes
    created_at: str


@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: str
    workspace_root: str
    title: str
    status: str
    created_at: str
    updated_at: str
    archived_at: str | None = None
    deleted_at: str | None = None
    purge_after: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SessionEvent:
    id: str
    session_id: str
    sequence: int
    type: str
    payload: dict[str, Any]
    schema_version: int
    created_at: str
    previous_event_hash: str | None
    event_hash: str
    turn_id: str | None = None
    idempotency_key: str | None = None
    source_session_id: str | None = None
    source_event_id: str | None = None
    blob_refs: tuple[BlobReference, ...] = ()


@dataclass(frozen=True, slots=True)
class MessagePart:
    kind: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SessionMessage:
    id: str
    event_id: str
    role: str
    parts: tuple[MessagePart, ...]
    created_at: str
    status: str = "complete"
    replayable: bool = True
    hidden: bool = False
    interruption_reason: str | None = None

    @property
    def content(self) -> str:
        return "".join(part.content for part in self.parts if part.kind == "text")


@dataclass(frozen=True, slots=True)
class SessionView:
    session_id: str
    metadata: dict[str, Any]
    transcript: tuple[SessionMessage, ...]
    model_messages: tuple[SessionMessage, ...]
    reset_sequence: int | None = None
