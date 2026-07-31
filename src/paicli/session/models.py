from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
    message_count: int = 0
    user_turn_count: int = 0
    latest_user_preview: str | None = None
    latest_assistant_preview: str | None = None
    provider: str | None = None
    model: str | None = None
    last_checkpoint_id: str | None = None
    last_compacted_at: str | None = None


@dataclass(frozen=True, slots=True)
class SessionRelationship:
    parent_session_id: str
    child_session_id: str
    relation_type: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolActionSpec:
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    raw_call: dict[str, Any]
    is_read_only: bool
    is_idempotent: bool


@dataclass(frozen=True, slots=True)
class PendingAction:
    session_id: str
    turn_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    raw_call: dict[str, Any]
    status: str
    is_read_only: bool
    is_idempotent: bool
    model_turn: int
    batch_index: int
    approval_status: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class SessionEvent:
    id: str
    session_id: str
    sequence: int
    type: str
    payload: dict[str, Any]
    schema_version: int
    created_at: str
    turn_id: str | None = None
    source_session_id: str | None = None
    source_event_id: str | None = None


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
        return "".join(part.content for part in self.parts if part.kind in {"text", "tool_result"})


@dataclass(frozen=True, slots=True)
class SessionView:
    session_id: str
    metadata: dict[str, Any]
    session_history: tuple[SessionMessage, ...]
    model_messages: tuple[SessionMessage, ...]
    reset_sequence: int | None = None
    context_checkpoint: dict[str, Any] | None = None
    context_checkpoint_sequence: int | None = None
