from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from paicli.clock import east_eight_now, format_timestamp
from paicli.session.manager import SessionManager
from paicli.session.models import (
    PendingAction,
    SessionEvent,
    SessionMessage,
    SessionRecord,
    SessionRelationship,
    SessionView,
    ToolActionSpec,
)
from paicli.session.replay import message_from_event, rebuild_session_view
from paicli.session.store import SessionStore
from paicli.session.validation import validate_event_payload
from paicli.usage import TokenUsage, UsageRecord

FORK_BOUNDARY_EVENT_TYPES = {"turn.completed", "turn.interrupted", "context.reset"}


class SessionRepository:
    """JSONL-backed Session domain operations.

    The directory is the store; every Session is an independent append-only JSONL file.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()
        self.store = SessionStore(self.root)

    def create_session(
        self,
        workspace_root: str | Path,
        *,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionRecord:
        session = self.store.create(workspace_root, title=title, metadata=metadata)
        self._append(
            session,
            "session.created",
            {
                "session_id": session.id,
                "title": session.header.title,
                "workspace_root": session.header.cwd,
                **dict(metadata or {}),
            },
        )
        return self._record(session)

    def get_or_create_root_session(
        self,
        workspace_root: str | Path,
        *,
        root_kind: str,
        title: str,
    ) -> SessionRecord:
        workspace = str(Path(workspace_root).expanduser().resolve())
        for record in self.list_sessions(include_archived=True, include_deleted=True):
            if (
                record.workspace_root == workspace
                and record.metadata.get("session_kind") == root_kind
            ):
                return record
        return self.create_session(
            workspace,
            title=title,
            metadata={"session_kind": root_kind},
        )

    def create_child_session(
        self,
        parent_session_id: str,
        *,
        relation_type: str,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
        **_: Any,
    ) -> SessionRecord:
        parent = self._manager(parent_session_id)
        child_metadata = {**dict(metadata or {}), "relation_type": relation_type}
        child = self.store.create(
            parent.header.cwd,
            title=title,
            parent_session=parent_session_id,
            metadata=child_metadata,
        )
        self._append(
            child,
            "session.created",
            {
                "session_id": child.id,
                "title": child.header.title,
                "workspace_root": child.header.cwd,
                **child_metadata,
            },
        )
        return self._record(child)

    def get_parent_relationship(self, child_session_id: str) -> SessionRelationship | None:
        child = self._manager(child_session_id)
        parent_id = child.header.parent_session
        if parent_id is None:
            return None
        return SessionRelationship(
            parent_session_id=parent_id,
            child_session_id=child.id,
            relation_type=str(child.header.metadata.get("relation_type") or "child"),
            created_at=child.header.timestamp,
            metadata=dict(child.header.metadata),
        )

    def list_child_sessions(
        self,
        parent_session_id: str,
        *,
        relation_type: str | None = None,
    ) -> tuple[SessionRecord, ...]:
        children = []
        for manager in self._all_managers():
            if manager.header.parent_session != parent_session_id:
                continue
            if relation_type and manager.header.metadata.get("relation_type") != relation_type:
                continue
            children.append(self._record(manager))
        children.sort(key=lambda record: record.created_at)
        return tuple(children)

    def append_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        turn_id: str | None = None,
        source_session_id: str | None = None,
        source_event_id: str | None = None,
        **_: Any,
    ) -> SessionEvent:
        validate_event_payload(event_type, payload)
        manager = self._manager(session_id)
        return self._append(
            manager,
            event_type,
            payload,
            turn_id=turn_id,
            source_session_id=source_session_id,
            source_event_id=source_event_id,
        )

    def append_message(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        reasoning_content: str | None = None,
        partial: bool = False,
        interruption_reason: str | None = None,
        turn_id: str | None = None,
        **_: Any,
    ) -> SessionMessage:
        event_type, payload = self._message_payload(
            session_id,
            role=role,
            content=content,
            partial=partial,
            interruption_reason=interruption_reason,
        )
        if reasoning_content:
            payload["parts"][0]["metadata"]["reasoning_content"] = reasoning_content
        event = self.append_event(
            session_id,
            event_type,
            payload,
            turn_id=turn_id,
        )
        return message_from_event(event)

    def begin_turn(
        self, session_id: str, *, turn_id: str, user_content: str, **_: Any
    ) -> SessionMessage:
        self.append_event(
            session_id,
            "turn.started",
            {},
            turn_id=turn_id,
        )
        return self.append_message(
            session_id,
            role="user",
            content=user_content,
            turn_id=turn_id,
        )

    def complete_turn(
        self,
        session_id: str,
        *,
        turn_id: str,
        assistant_content: str,
        reasoning_content: str | None = None,
        context_checkpoint: dict[str, Any] | None = None,
        **_: Any,
    ) -> SessionMessage:
        message = self.append_message(
            session_id,
            role="assistant",
            content=assistant_content,
            reasoning_content=reasoning_content,
            turn_id=turn_id,
        )
        self.append_event(
            session_id,
            "turn.completed",
            {},
            turn_id=turn_id,
        )
        if context_checkpoint is not None:
            self.save_context_checkpoint(session_id, context_checkpoint, turn_id=turn_id)
        return message

    def interrupt_turn(
        self,
        session_id: str,
        *,
        turn_id: str,
        assistant_content: str,
        reasoning_content: str | None = None,
        reason: str,
        **_: Any,
    ) -> SessionMessage | None:
        if any(
            event.turn_id == turn_id and event.type in {"turn.completed", "turn.interrupted"}
            for event in self.list_events(session_id)
        ):
            return None
        for action in self.list_pending_actions(session_id):
            if action.turn_id == turn_id:
                self.abandon_tool_action(session_id, action.tool_call_id, reason=reason)
        message = (
            self.append_message(
                session_id,
                role="assistant",
                content=assistant_content,
                reasoning_content=reasoning_content,
                partial=True,
                interruption_reason=reason,
                turn_id=turn_id,
            )
            if assistant_content or reasoning_content
            else None
        )
        self.append_event(
            session_id,
            "turn.interrupted",
            {"reason": reason},
            turn_id=turn_id,
        )
        return message

    def active_turn_id(self, session_id: str) -> str | None:
        events = self.list_events(session_id)
        terminal_turns = {
            event.turn_id
            for event in events
            if event.type in {"turn.completed", "turn.interrupted"}
        }
        return next(
            (
                event.turn_id
                for event in reversed(events)
                if event.type == "turn.started"
                and event.turn_id
                and event.turn_id not in terminal_turns
            ),
            None,
        )

    def interrupt_active_turn(self, session_id: str, *, reason: str) -> bool:
        turn_id = self.active_turn_id(session_id)
        if turn_id is None:
            return False
        self.interrupt_turn(
            session_id,
            turn_id=turn_id,
            assistant_content="",
            reason=reason,
        )
        return True

    def prepare_tool_actions(
        self,
        session_id: str,
        *,
        turn_id: str,
        model_turn: int,
        assistant_content: str,
        reasoning_content: str | None = None,
        actions: tuple[ToolActionSpec, ...],
        **_: Any,
    ) -> tuple[PendingAction, ...]:
        if not actions:
            raise ValueError("tool action batch must not be empty")
        event_type, message_payload = self._message_payload(
            session_id,
            role="assistant",
            content=assistant_content,
        )
        if reasoning_content:
            message_payload["parts"][0]["metadata"]["reasoning_content"] = reasoning_content
        for action in actions:
            message_payload["parts"].append(
                {
                    "kind": "tool_call",
                    "content": "",
                    "metadata": {
                        "tool_call_id": action.tool_call_id,
                        "tool_name": action.tool_name,
                        "arguments": action.arguments,
                        "raw_call": action.raw_call,
                    },
                }
            )
        self.append_event(
            session_id,
            event_type,
            message_payload,
            turn_id=turn_id,
        )
        for index, action in enumerate(actions):
            self.append_event(
                session_id,
                "pending_action.prepared",
                {
                    "tool_call_id": action.tool_call_id,
                    "tool_name": action.tool_name,
                    "arguments": action.arguments,
                    "raw_call": action.raw_call,
                    "is_read_only": action.is_read_only,
                    "is_idempotent": action.is_idempotent,
                    "model_turn": model_turn,
                    "batch_index": index,
                    "status": "prepared",
                },
                turn_id=turn_id,
            )
        action_ids = {action.tool_call_id for action in actions}
        return tuple(
            action
            for action in self.list_pending_actions(session_id)
            if action.tool_call_id in action_ids
        )

    def list_pending_actions(
        self,
        session_id: str,
        *,
        include_settled: bool = False,
    ) -> list[PendingAction]:
        states: dict[str, dict[str, Any]] = {}
        for event in self.list_events(session_id):
            call_id = str(event.payload.get("tool_call_id") or "")
            if not call_id:
                continue
            if event.type == "pending_action.prepared":
                states[call_id] = {
                    **event.payload,
                    "turn_id": event.turn_id or "",
                    "created_at": event.created_at,
                    "updated_at": event.created_at,
                    "approval_status": None,
                }
            elif call_id in states:
                state = states[call_id]
                state["updated_at"] = event.created_at
                if event.type == "pending_action.started":
                    state["status"] = "executing"
                elif event.type == "approval.requested":
                    state["status"] = "waiting_approval"
                    state["approval_status"] = "requested"
                elif event.type == "approval.resolved":
                    state["status"] = "prepared" if event.payload.get("deferred") else "executing"
                    state["approval_status"] = event.payload.get("decision")
                elif event.type == "pending_action.completed":
                    state["status"] = "completed"
                elif event.type == "pending_action.abandoned":
                    state["status"] = "abandoned"
        actions = [self._pending_action(session_id, state) for state in states.values()]
        if not include_settled:
            actions = [
                action for action in actions if action.status not in {"completed", "abandoned"}
            ]
        actions.sort(key=lambda action: (action.model_turn, action.batch_index))
        return actions

    def start_tool_action(self, session_id: str, tool_call_id: str, **_: Any) -> PendingAction:
        action = self._require_pending_action(session_id, tool_call_id)
        self.append_event(
            session_id,
            "pending_action.started",
            {"tool_call_id": tool_call_id},
            turn_id=action.turn_id,
        )
        return self._require_pending_action(session_id, tool_call_id)

    def request_tool_approval(self, session_id: str, tool_call_id: str, **_: Any) -> PendingAction:
        action = self._require_pending_action(session_id, tool_call_id)
        self.append_event(
            session_id,
            "approval.requested",
            {"tool_call_id": tool_call_id, "tool_name": action.tool_name},
            turn_id=action.turn_id,
        )
        return self._require_pending_action(session_id, tool_call_id)

    def resolve_tool_approval(
        self,
        session_id: str,
        tool_call_id: str,
        *,
        decision: str,
        deferred_execution: bool = False,
        **_: Any,
    ) -> PendingAction:
        if decision not in {"approve", "allow_session", "deny", "skip"}:
            raise ValueError(f"unsupported tool approval decision: {decision}")
        action = self._require_pending_action(session_id, tool_call_id)
        if action.approval_status != "requested":
            raise ValueError(f"tool action is not waiting for approval: {tool_call_id}")
        self.append_event(
            session_id,
            "approval.resolved",
            {
                "tool_call_id": tool_call_id,
                "decision": decision,
                "deferred": deferred_execution,
            },
            turn_id=action.turn_id,
        )
        return self._require_pending_action(session_id, tool_call_id)

    def complete_tool_action(
        self,
        session_id: str,
        tool_call_id: str,
        *,
        content: str,
        is_error: bool,
        **_: Any,
    ) -> SessionMessage:
        action = self._require_pending_action(session_id, tool_call_id)
        event_type, payload = self._message_payload(
            session_id,
            role="tool",
            content=content,
        )
        payload["parts"][0] = {
            "kind": "tool_result",
            "content": content,
            "metadata": {
                "tool_call_id": tool_call_id,
                "tool_name": action.tool_name,
                "is_error": is_error,
                "execution_outcome": "error" if is_error else "completed",
            },
        }
        event = self.append_event(
            session_id,
            event_type,
            payload,
            turn_id=action.turn_id,
        )
        self.append_event(
            session_id,
            "pending_action.completed",
            {"tool_call_id": tool_call_id, "is_error": is_error},
            turn_id=action.turn_id,
        )
        return message_from_event(event)

    def abandon_tool_action(
        self,
        session_id: str,
        tool_call_id: str,
        *,
        reason: str,
        **_: Any,
    ) -> SessionMessage:
        action = self._require_pending_action(session_id, tool_call_id)
        content = (
            f'Tool "{action.tool_name}" has an unknown execution outcome after {reason}; '
            "inspect current state before retrying."
        )
        event_type, payload = self._message_payload(
            session_id,
            role="tool",
            content=content,
        )
        payload["parts"][0] = {
            "kind": "tool_result",
            "content": content,
            "metadata": {
                "tool_call_id": tool_call_id,
                "tool_name": action.tool_name,
                "is_error": True,
                "execution_outcome": "unknown",
            },
        }
        message_event = self.append_event(
            session_id,
            event_type,
            payload,
            turn_id=action.turn_id,
        )
        self.append_event(
            session_id,
            "pending_action.abandoned",
            {"tool_call_id": tool_call_id, "reason": reason, "execution_outcome": "unknown"},
            turn_id=action.turn_id,
        )
        return message_from_event(message_event)

    def record_usage(
        self,
        session_id: str,
        record: UsageRecord,
        *,
        turn_id: str,
        **_: Any,
    ) -> SessionEvent:
        return self.append_event(
            session_id,
            "usage.recorded",
            record.to_payload(),
            turn_id=turn_id,
        )

    def save_context_checkpoint(
        self,
        session_id: str,
        checkpoint: dict[str, Any],
        *,
        turn_id: str | None = None,
        **_: Any,
    ) -> bool:
        checkpoint_id = str(checkpoint["checkpoint_id"])
        if any(
            event.type == "context.checkpoint_created"
            and event.payload.get("checkpoint_id") == checkpoint_id
            for event in self.list_events(session_id)
        ):
            return False
        compaction = dict(checkpoint.get("compaction") or {})
        checkpoint_messages = [
            dict(message)
            for message in list(checkpoint.get("messages") or [])
            if isinstance(message, dict)
        ]
        session_messages = iter(self.rebuild_session_view(session_id).model_messages)
        current_session_message = next(session_messages, None)
        for checkpoint_message in checkpoint_messages:
            while current_session_message is not None:
                content = checkpoint_message.get("content", "")
                if (
                    checkpoint_message.get("role") == current_session_message.role
                    and isinstance(content, str)
                    and content == current_session_message.content
                ):
                    checkpoint_message["source_message_id"] = current_session_message.id
                    current_session_message = next(session_messages, None)
                    break
                current_session_message = next(session_messages, None)
        self.append_event(
            session_id,
            "context.compacted",
            {
                "checkpoint_id": checkpoint_id,
                "summary": str(checkpoint.get("summary") or "Compacted context"),
                "compaction": compaction,
                "pressure": dict(checkpoint.get("pressure") or {}),
                "provider": str(checkpoint.get("provider") or ""),
                "model": str(checkpoint.get("model") or ""),
            },
            turn_id=turn_id,
        )
        self.append_event(
            session_id,
            "context.checkpoint_created",
            {
                "checkpoint_id": checkpoint_id,
                "messages": checkpoint_messages,
            },
            turn_id=turn_id,
        )
        return True

    def save_context_summary_usage(
        self,
        session_id: str,
        *,
        provider: str,
        model: str,
        records: list[dict[str, Any]],
        **_: Any,
    ) -> None:
        for index, raw in enumerate(records, start=1):
            record = UsageRecord(
                usage_id=f"manual-context-summary:{uuid4().hex}:{index}",
                request_id=None,
                provider=provider,
                model=model,
                purpose="context_summary",
                tokens=TokenUsage(
                    input_tokens=int(raw.get("input_tokens") or 0),
                    output_tokens=int(raw.get("output_tokens") or 0),
                    cache_read_tokens=int(raw.get("cache_read_tokens") or 0),
                    cache_write_tokens=int(raw.get("cache_write_tokens") or 0),
                ),
                cost=None,
                usage_source=("estimated" if raw.get("usage_source") == "estimated" else "actual"),
            )
            self.record_usage(session_id, record, turn_id=f"compact_{uuid4().hex}")

    def reset_context(self, session_id: str, **_: Any) -> SessionEvent:
        return self.append_event(session_id, "context.reset", {})

    def hide_message(self, session_id: str, message_id: str, **_: Any) -> SessionEvent:
        if not any(
            message.id == message_id
            for message in self.rebuild_session_view(session_id).session_history
        ):
            raise KeyError(f"message not found in session {session_id}: {message_id}")
        return self.append_event(session_id, "message.hidden", {"message_id": message_id})

    def update_session_metadata(
        self,
        session_id: str,
        *,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
        **_: Any,
    ) -> SessionRecord:
        payload = dict(metadata or {})
        if title is not None:
            payload["title"] = title
        if payload:
            self.append_event(session_id, "session.metadata_updated", payload)
        return self._record(self._manager(session_id))

    def archive_session(self, session_id: str, **_: Any) -> SessionRecord:
        self._append(self._manager(session_id), "session.archived", {})
        return self._record(self._manager(session_id))

    def unarchive_session(self, session_id: str, **_: Any) -> SessionRecord:
        self._append(self._manager(session_id), "session.unarchived", {})
        return self._record(self._manager(session_id))

    def delete_session(
        self, session_id: str, *, retention_days: int = 30, **_: Any
    ) -> SessionRecord:
        purge_after = format_timestamp(east_eight_now() + timedelta(days=retention_days))
        self._append(self._manager(session_id), "session.deleted", {"purge_after": purge_after})
        return self._record(self._manager(session_id))

    def restore_session(self, session_id: str, **_: Any) -> SessionRecord:
        self._append(self._manager(session_id), "session.restored", {})
        return self._record(self._manager(session_id))

    def purge_session(self, session_id: str) -> bool:
        try:
            manager = self._manager(session_id)
        except KeyError:
            return False
        manager.path.unlink()
        return True

    def fork_session(
        self,
        source_session_id: str,
        *,
        through_sequence: int | None = None,
        workspace_root: str | Path | None = None,
        title: str | None = None,
        **_: Any,
    ) -> SessionRecord:
        source = self._manager(source_session_id)
        events = self.list_events(source_session_id)
        if not events:
            raise ValueError("session has no completed turn boundary")
        if through_sequence is None:
            boundary = next(
                (event for event in reversed(events) if event.type in FORK_BOUNDARY_EVENT_TYPES),
                None,
            )
            if boundary is None:
                raise ValueError("session has no completed turn boundary")
            through_sequence = boundary.sequence
        if through_sequence < 1 or through_sequence > len(events):
            raise ValueError(f"invalid fork sequence: {through_sequence}")
        if events[through_sequence - 1].type not in FORK_BOUNDARY_EVENT_TYPES:
            raise ValueError(f"fork sequence is not a completed turn boundary: {through_sequence}")
        child = self.store.create(
            workspace_root or source.header.cwd,
            title=title or f"{self._record(source).title} (fork)",
            parent_session=source_session_id,
            metadata=dict(source.header.metadata),
        )
        self._append(
            child,
            "session.created",
            {
                "session_id": child.id,
                "title": child.header.title,
                "workspace_root": child.header.cwd,
                **dict(child.header.metadata),
            },
        )
        self._append(
            child,
            "session.forked",
            {"source_session_id": source_session_id, "source_through_sequence": through_sequence},
            source_session_id=source_session_id,
        )
        for event in events[:through_sequence]:
            if not _forkable(event):
                continue
            self._append(
                child,
                event.type,
                event.payload,
                turn_id=event.turn_id,
                source_session_id=source_session_id,
                source_event_id=event.id,
            )
        return self._record(child)

    def get_session(self, session_id: str) -> SessionRecord | None:
        try:
            return self._record(self._manager(session_id))
        except KeyError:
            return None

    def list_sessions(
        self,
        *,
        include_archived: bool = False,
        include_deleted: bool = False,
        **_: Any,
    ) -> list[SessionRecord]:
        managers = self._all_managers()
        managers.sort(
            key=lambda manager: (manager.updated_at, manager.modified_ns),
            reverse=True,
        )
        records = [self._record(manager) for manager in managers]
        if not include_archived:
            records = [record for record in records if record.archived_at is None]
        if not include_deleted:
            records = [record for record in records if record.deleted_at is None]
        return records

    def list_events(self, session_id: str) -> list[SessionEvent]:
        manager = self._manager(session_id)
        events = []
        for sequence, entry in enumerate(manager.current_branch(), start=1):
            raw = entry.data
            events.append(
                SessionEvent(
                    id=entry.id,
                    session_id=session_id,
                    sequence=sequence,
                    type=entry.type,
                    payload=dict(raw.get("payload") or {}),
                    schema_version=1,
                    created_at=entry.timestamp,
                    turn_id=_optional_string(raw.get("turnId")),
                    source_session_id=_optional_string(raw.get("sourceSessionId")),
                    source_event_id=_optional_string(raw.get("sourceEventId")),
                )
            )
        return events

    def rebuild_session_view(self, session_id: str) -> SessionView:
        return rebuild_session_view(session_id, self.list_events(session_id))

    def verify_session(self, session_id: str) -> None:
        self._manager(session_id)

    def _append(
        self,
        manager: SessionManager,
        event_type: str,
        payload: dict[str, Any],
        *,
        turn_id: str | None = None,
        source_session_id: str | None = None,
        source_event_id: str | None = None,
    ) -> SessionEvent:
        data: dict[str, Any] = {"payload": payload}
        if turn_id is not None:
            data["turnId"] = turn_id
        if source_session_id is not None:
            data["sourceSessionId"] = source_session_id
        if source_event_id is not None:
            data["sourceEventId"] = source_event_id
        entry_id = manager.append(event_type, data)
        return next(event for event in self.list_events(manager.id) if event.id == entry_id)

    def _manager(self, session_id: str) -> SessionManager:
        return self.store.open(session_id)

    def _all_managers(self) -> list[SessionManager]:
        return list(self.store.all())

    def _record(self, manager: SessionManager) -> SessionRecord:
        title = manager.header.title
        metadata = dict(manager.header.metadata)
        archived_at = None
        deleted_at = None
        purge_after = None
        messages: list[SessionEvent] = []
        provider = None
        model = None
        last_checkpoint_id = None
        last_compacted_at = None
        for event in self.list_events(manager.id):
            if event.type == "session.created":
                metadata.update(event.payload)
                title = str(event.payload.get("title") or title)
            elif event.type == "session.metadata_updated":
                if event.payload.get("title"):
                    title = str(event.payload["title"])
                for key, value in event.payload.items():
                    if key == "title":
                        continue
                    if value is None:
                        metadata.pop(key, None)
                    else:
                        metadata[key] = value
            elif event.type == "session.archived":
                archived_at = event.created_at
            elif event.type == "session.unarchived":
                archived_at = None
            elif event.type == "session.deleted":
                deleted_at = event.created_at
                purge_after = _optional_string(event.payload.get("purge_after"))
            elif event.type == "session.restored":
                deleted_at = None
                purge_after = None
            elif event.type.startswith("message.") and event.type != "message.hidden":
                messages.append(event)
            elif event.type == "usage.recorded":
                provider = _optional_string(event.payload.get("provider"))
                model = _optional_string(event.payload.get("model"))
            elif event.type == "context.checkpoint_created":
                last_checkpoint_id = _optional_string(event.payload.get("checkpoint_id"))
            elif event.type == "context.compacted":
                last_compacted_at = event.created_at
        user_messages = [event for event in messages if event.payload.get("role") == "user"]
        assistant_messages = [
            event for event in messages if event.payload.get("role") == "assistant"
        ]
        return SessionRecord(
            id=manager.id,
            workspace_root=manager.header.cwd,
            title=title,
            status="idle",
            created_at=manager.header.timestamp,
            updated_at=manager.updated_at,
            archived_at=archived_at,
            deleted_at=deleted_at,
            purge_after=purge_after,
            metadata=metadata,
            message_count=len(messages),
            user_turn_count=len(user_messages),
            latest_user_preview=_preview(user_messages[-1]) if user_messages else None,
            latest_assistant_preview=_preview(assistant_messages[-1])
            if assistant_messages
            else None,
            provider=provider,
            model=model,
            last_checkpoint_id=last_checkpoint_id,
            last_compacted_at=last_compacted_at,
        )

    def _message_payload(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        partial: bool = False,
        interruption_reason: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        if role not in {"user", "assistant", "tool"}:
            raise ValueError(f"unsupported message role: {role}")
        message_id = f"msg_{uuid4().hex}"
        payload: dict[str, Any] = {
            "message_id": message_id,
            "role": role,
            "parts": [{"kind": "text", "content": content, "metadata": {}}],
            "status": "partial" if partial else "complete",
            "replayable": not partial,
        }
        if interruption_reason is not None:
            payload["interruption_reason"] = interruption_reason
        return ("message.assistant.partial" if partial else f"message.{role}", payload)

    def _require_pending_action(self, session_id: str, tool_call_id: str) -> PendingAction:
        action = next(
            (
                item
                for item in self.list_pending_actions(session_id, include_settled=True)
                if item.tool_call_id == tool_call_id
            ),
            None,
        )
        if action is None:
            raise KeyError(f"pending tool action not found: {tool_call_id}")
        if action.status in {"completed", "abandoned"}:
            raise ValueError(f"tool action is already settled: {tool_call_id}")
        return action

    @staticmethod
    def _pending_action(session_id: str, state: dict[str, Any]) -> PendingAction:
        return PendingAction(
            session_id=session_id,
            turn_id=str(state["turn_id"]),
            tool_call_id=str(state["tool_call_id"]),
            tool_name=str(state["tool_name"]),
            arguments=dict(state.get("arguments") or {}),
            raw_call=dict(state.get("raw_call") or {}),
            status=str(state.get("status") or "prepared"),
            is_read_only=bool(state.get("is_read_only")),
            is_idempotent=bool(state.get("is_idempotent")),
            model_turn=int(state.get("model_turn") or 0),
            batch_index=int(state.get("batch_index") or 0),
            approval_status=_optional_string(state.get("approval_status")),
            created_at=str(state["created_at"]),
            updated_at=str(state["updated_at"]),
        )


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


def _preview(event: SessionEvent) -> str:
    parts = event.payload.get("parts") or []
    text = "".join(
        str(part.get("content") or "")
        for part in parts
        if isinstance(part, dict) and part.get("kind") in {None, "text", "tool_result"}
    )
    return text[:200]


def _forkable(event: SessionEvent) -> bool:
    return not event.type.startswith(("approval.", "pending_action.", "turn.", "session."))
