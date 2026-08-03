from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from paicli.session.jsonl_repository import SessionRepository
from paicli.session.models import SessionMessage, SessionRecord, ToolActionSpec
from paicli.session.stats import SessionStats, calculate_session_stats
from paicli.types import Message, Role
from paicli.usage import UsageRecord


def default_session_directory() -> Path:
    return Path.home() / ".paicli" / "sessions"


class InteractiveSession:
    """Bind one interactive frontend to a durable workspace session."""

    def __init__(
        self,
        repository: SessionRepository,
        workspace_root: str | Path,
        *,
        session_id: str | None = None,
    ) -> None:
        self.repository = repository
        self.workspace_root = str(Path(workspace_root).expanduser().resolve())
        self.record = self._open_session(session_id)
        self._active_turn_id = self._find_active_turn_id()
        self._stats_snapshot = self.refresh_stats()

    @property
    def id(self) -> str:
        return self.record.id

    @property
    def session_history(self) -> tuple[SessionMessage, ...]:
        return self.repository.rebuild_session_view(self.id).session_history

    @property
    def stats(self) -> SessionStats:
        return self.refresh_stats()

    @property
    def stats_snapshot(self) -> SessionStats:
        return self._stats_snapshot

    def refresh_stats(self) -> SessionStats:
        stats = calculate_session_stats(self.repository.list_events(self.id))
        self._stats_snapshot = stats
        return stats

    @property
    def agent_history(self) -> list[Message]:
        view = self.repository.rebuild_session_view(self.id)
        checkpoint = view.context_checkpoint
        checkpoint_sequence = view.context_checkpoint_sequence
        if checkpoint is None or checkpoint_sequence is None:
            return [_agent_message(message) for message in view.model_messages]

        checkpoint_messages = checkpoint.get("messages")
        if not isinstance(checkpoint_messages, list):
            raise ValueError("context checkpoint messages must be a list")
        events = self.repository.list_events(self.id)
        event_sequences = {event.id: event.sequence for event in events}
        hidden_message_ids = {
            str(event.payload.get("message_id") or "")
            for event in events
            if event.sequence > checkpoint_sequence and event.type == "message.hidden"
        }
        checkpoint_entries = [
            (item, _agent_message_from_payload(item))
            for item in checkpoint_messages
            if isinstance(item, dict)
        ]
        checkpoint_history = [
            message
            for payload, message in checkpoint_entries
            if str(payload.get("source_message_id") or "") not in hidden_message_ids
        ]
        annotated_message_ids = {
            str(payload.get("source_message_id") or "")
            for payload, _message in checkpoint_entries
            if payload.get("source_message_id")
        }
        messages_by_id = {message.id: message for message in view.session_history}
        for event in events:
            if event.sequence <= checkpoint_sequence or event.type != "message.hidden":
                continue
            hidden_id = str(event.payload.get("message_id") or "")
            if hidden_id in annotated_message_ids:
                continue
            hidden = messages_by_id.get(hidden_id)
            if hidden is None or event_sequences.get(hidden.event_id, 0) > checkpoint_sequence:
                continue
            hidden_message = _agent_message(hidden)
            matching_index = next(
                (
                    index
                    for index in range(len(checkpoint_history) - 1, -1, -1)
                    if checkpoint_history[index] == hidden_message
                ),
                None,
            )
            if matching_index is not None:
                checkpoint_history.pop(matching_index)
        tail = [
            _agent_message(message)
            for message in view.model_messages
            if event_sequences.get(message.event_id, 0) > checkpoint_sequence
        ]
        return checkpoint_history + tail

    def restore_agent_history(self, agent: Any) -> None:
        history = self.agent_history
        checkpoint = self.repository.rebuild_session_view(self.id).context_checkpoint
        restore_context = getattr(agent, "restore_session_context", None)
        if checkpoint is not None and callable(restore_context):
            restore_context(history, checkpoint)
            return
        agent.replace_history(history)

    def discard_incomplete_turn(self, *, reason: str) -> bool:
        if self._active_turn_id is None:
            return False
        self.interrupt_turn("", reason=reason)
        return True

    def prepare_background_task_recovery_state(self) -> dict[str, Any] | None:
        if self._active_turn_id is None:
            return None
        all_actions = [
            action
            for action in self.repository.list_pending_actions(
                self.id,
                include_settled=True,
            )
            if action.turn_id == self._active_turn_id
        ]
        if not all_actions:
            self.interrupt_turn(
                "",
                reason="process_restarted_before_tool_state",
            )
            return None
        pending = [
            action for action in all_actions if action.status not in {"completed", "abandoned"}
        ]
        retryable = []
        for action in pending:
            if action.approval_status in {"deny", "skip"}:
                self.repository.complete_tool_action(
                    self.id,
                    action.tool_call_id,
                    content=(
                        f'Tool "{action.tool_name}" was '
                        f"{'denied' if action.approval_status == 'deny' else 'skipped'} "
                        "by approval policy."
                    ),
                    is_error=True,
                )
            elif action.status in {"prepared", "waiting_approval"} or (
                action.status == "executing" and action.is_read_only and action.is_idempotent
            ):
                retryable.append(action)
            else:
                self.repository.abandon_tool_action(
                    self.id,
                    action.tool_call_id,
                    reason="process_restarted",
                )
        view = self.repository.rebuild_session_view(self.id)
        return {
            "messages": [_agent_message_dict(message) for message in view.model_messages],
            "pending_tool_calls": [action.raw_call for action in retryable],
            "approval_decisions": {
                action.tool_call_id: action.approval_status
                for action in retryable
                if action.approval_status in {"approve", "allow_session"}
            },
            "next_tool_index": 0,
            "total_tokens": 0,
            "turn": max((action.model_turn for action in all_actions), default=0),
            "tool_call_count": len(all_actions),
            "finalizing": False,
            "limit_reason": "",
            "last_signature": "",
            "repeated_batches": 0,
            "last_actual_usage": None,
        }

    def record_tool_batch(
        self,
        *,
        model_turn: int,
        assistant_content: str,
        reasoning_content: str | None,
        reasoning_duration: float | None = None,
        actions: list[dict[str, Any]],
    ) -> None:
        turn_id = self._require_active_turn()
        self.repository.prepare_tool_actions(
            self.id,
            turn_id=turn_id,
            model_turn=model_turn,
            assistant_content=assistant_content,
            reasoning_content=reasoning_content,
            reasoning_duration=reasoning_duration,
            actions=tuple(
                ToolActionSpec(
                    tool_call_id=str(action["tool_call_id"]),
                    tool_name=str(action["tool_name"]),
                    arguments=dict(action.get("arguments") or {}),
                    raw_call=dict(action.get("raw_call") or {}),
                    is_read_only=bool(action.get("is_read_only")),
                    is_idempotent=bool(action.get("is_idempotent")),
                )
                for action in actions
            ),
        )

    def start_tool_action(self, tool_call_id: str) -> None:
        self.repository.start_tool_action(
            self.id,
            tool_call_id,
        )

    def complete_tool_action(
        self,
        tool_call_id: str,
        *,
        content: str,
        is_error: bool,
    ) -> None:
        self.repository.complete_tool_action(
            self.id,
            tool_call_id,
            content=content,
            is_error=is_error,
        )

    def record_tool_output(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        text: str,
        stream: str,
    ) -> None:
        """Persist live tool output without adding it to model messages."""
        if not text:
            return
        self.repository.append_event(
            self.id,
            "tool_output_delta",
            {
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "text": text,
                "stream": stream,
            },
            turn_id=self._require_active_turn(),
        )

    def record_usage(self, record: UsageRecord) -> None:
        turn_id = self._require_active_turn()
        self.repository.record_usage(
            self.id,
            record,
            turn_id=turn_id,
        )
        self.refresh_stats()

    def request_tool_approval(self, request: dict[str, Any]) -> str:
        requested_call_id = str(request.get("tool_call_id") or "")
        tool_name = str(request.get("tool_name") or "")
        arguments = request.get("input")
        candidates = [
            action
            for action in self.repository.list_pending_actions(self.id)
            if (not requested_call_id or action.tool_call_id == requested_call_id)
            and action.tool_name == tool_name
            and action.arguments == (arguments if isinstance(arguments, dict) else {})
        ]
        if len(candidates) != 1:
            raise RuntimeError("approval request does not match one pending tool action")
        tool_call_id = candidates[0].tool_call_id
        self.repository.request_tool_approval(
            self.id,
            tool_call_id,
        )
        return tool_call_id

    def resolve_tool_approval(
        self,
        tool_call_id: str,
        decision: str,
        *,
        deferred_execution: bool = False,
    ) -> None:
        self.repository.resolve_tool_approval(
            self.id,
            tool_call_id,
            decision=decision,
            deferred_execution=deferred_execution,
        )

    def begin_turn(self, message: str) -> str:
        if self._active_turn_id is not None:
            raise RuntimeError("session already has an active turn")
        turn_id = f"turn_{uuid4().hex}"
        self.repository.begin_turn(
            self.id,
            turn_id=turn_id,
            user_content=message,
        )
        self._active_turn_id = turn_id
        return turn_id

    def complete_turn(
        self,
        assistant_text: str,
        *,
        reasoning_content: str | None = None,
        reasoning_duration: float | None = None,
        context_checkpoint: dict[str, Any] | None = None,
    ) -> None:
        turn_id = self._require_active_turn()
        self.repository.complete_turn(
            self.id,
            turn_id=turn_id,
            assistant_content=assistant_text,
            reasoning_content=reasoning_content,
            reasoning_duration=reasoning_duration,
            context_checkpoint=context_checkpoint,
        )
        self._active_turn_id = None
        self.refresh_stats()

    def interrupt_turn(
        self,
        assistant_text: str,
        *,
        reasoning_content: str | None = None,
        reasoning_duration: float | None = None,
        reason: str,
    ) -> None:
        if self._active_turn_id is None:
            return
        turn_id = self._active_turn_id
        self.repository.interrupt_turn(
            self.id,
            turn_id=turn_id,
            assistant_content=assistant_text,
            reasoning_content=reasoning_content,
            reasoning_duration=reasoning_duration,
            reason=reason,
        )
        self._active_turn_id = None
        self.refresh_stats()

    def reset_context(self) -> None:
        if self._active_turn_id is not None:
            raise RuntimeError("cannot reset context during an active turn")
        self.repository.reset_context(self.id)

    def save_context_checkpoint(self, checkpoint: dict[str, Any]) -> bool:
        if self._active_turn_id is not None:
            raise RuntimeError("cannot compact context during an active turn")
        return self.repository.save_context_checkpoint(
            self.id,
            checkpoint,
        )

    def save_context_summary_usage(
        self,
        *,
        provider: str,
        model: str,
        records: list[dict[str, Any]],
    ) -> None:
        if self._active_turn_id is not None:
            raise RuntimeError("cannot record manual compaction during an active turn")
        self.repository.save_context_summary_usage(
            self.id,
            provider=provider,
            model=model,
            records=records,
        )
        self.refresh_stats()

    def list_sessions(self) -> tuple[SessionRecord, ...]:
        return tuple(
            record
            for record in self.repository.list_sessions(
                include_archived=True,
                include_deleted=True,
            )
            if record.workspace_root == self.workspace_root
        )

    def rename_session(self, title: str) -> SessionRecord:
        self._require_idle()
        normalized = " ".join(title.split())
        if not normalized:
            raise ValueError("session title must be non-empty")
        self.record = self.repository.update_session_metadata(
            self.id,
            title=normalized,
        )
        return self.record

    def show_session(self, session_id: str | None = None) -> SessionRecord:
        resolved_id = session_id or self.id
        record = self.repository.get_session(resolved_id)
        if record is None:
            raise KeyError(f"session not found: {resolved_id}")
        if record.workspace_root != self.workspace_root:
            raise ValueError(f"session belongs to another workspace: {resolved_id}")
        return record

    def resume_candidates(self) -> tuple[SessionRecord, ...]:
        return tuple(
            record
            for record in self.list_sessions()
            if record.id != self.id and record.deleted_at is None and record.status != "corrupt"
        )

    def new_session(self, *, title: str | None = None) -> SessionRecord:
        self._require_idle()
        record = self.repository.create_session(
            self.workspace_root,
            title=title,
        )
        self._switch_to(record)
        return self.record

    def resume_session(self, session_id: str) -> SessionRecord:
        self._require_idle()
        record = self._resolve_workspace_session(session_id, allow_archived=True)
        if record.archived_at is not None:
            record = self.repository.unarchive_session(session_id)
        self._switch_to(record)
        return self.record

    def fork_session(self, *, title: str | None = None) -> SessionRecord:
        self._require_idle()
        record = self.repository.fork_session(
            self.id,
            workspace_root=self.workspace_root,
            title=title,
        )
        self._switch_to(record)
        return self.record

    def archive_session(self) -> tuple[SessionRecord, SessionRecord]:
        self._require_idle()
        archived = self.repository.archive_session(self.id)
        replacement = self.repository.create_session(self.workspace_root)
        self._switch_to(replacement)
        return archived, replacement

    def delete_session(self) -> tuple[SessionRecord, SessionRecord]:
        self._require_idle()
        deleted = self.repository.delete_session(self.id)
        replacement = self.repository.create_session(self.workspace_root)
        self._switch_to(replacement)
        return deleted, replacement

    def restore_session(self, session_id: str) -> SessionRecord:
        self._require_idle()
        record = self._resolve_workspace_session(
            session_id,
            allow_archived=True,
            allow_deleted=True,
        )
        if record.deleted_at is None:
            raise ValueError(f"session is not deleted: {session_id}")
        restored = self.repository.restore_session(session_id)
        self._switch_to(restored)
        return self.record

    def close(self) -> None:
        """Close the Session lifecycle hook; JSONL writers hold no persistent resource."""

    def _require_active_turn(self) -> str:
        if self._active_turn_id is None:
            raise RuntimeError("session has no active turn")
        return self._active_turn_id

    def _require_idle(self) -> None:
        if self._active_turn_id is not None:
            raise RuntimeError("cannot switch sessions during an active turn")

    def _open_session(
        self,
        session_id: str | None,
    ) -> SessionRecord:
        if session_id is not None:
            return self._resolve_workspace_session(session_id)
        for record in self.repository.list_sessions():
            if record.workspace_root != self.workspace_root or record.status == "corrupt":
                continue
            return record
        return self.repository.create_session(self.workspace_root)

    def _switch_to(
        self,
        record: SessionRecord,
    ) -> None:
        if record.id == self.id:
            return
        self.record = record
        self._active_turn_id = self._find_active_turn_id()
        self.refresh_stats()

    def _find_active_turn_id(self) -> str | None:
        active: str | None = None
        for event in self.repository.list_events(self.id):
            if event.type == "turn.started":
                active = event.turn_id
            elif event.type in {"turn.completed", "turn.interrupted"} and event.turn_id == active:
                active = None
        return active

    def _resolve_workspace_session(
        self,
        session_id: str,
        *,
        allow_archived: bool = False,
        allow_deleted: bool = False,
    ) -> SessionRecord:
        record = self.repository.get_session(session_id)
        if record is None:
            raise KeyError(f"session not found: {session_id}")
        if record.workspace_root != self.workspace_root:
            raise ValueError(f"session belongs to another workspace: {session_id}")
        if record.status == "corrupt":
            raise ValueError(f"session is corrupt: {session_id}")
        if record.archived_at is not None and not allow_archived:
            raise ValueError(f"session is archived: {session_id}")
        if record.deleted_at is not None and not allow_deleted:
            raise ValueError(f"session is deleted: {session_id}")
        return record


def _agent_message(message: SessionMessage) -> Message:
    tool_calls = [
        dict(part.metadata.get("raw_call") or {})
        for part in message.parts
        if part.kind == "tool_call"
    ]
    tool_result = next(
        (part for part in message.parts if part.kind == "tool_result"),
        None,
    )
    text_part = next((part for part in message.parts if part.kind == "text"), None)
    return Message(
        role=cast(Role, message.role),
        content=message.content,
        tool_call_id=(
            str(tool_result.metadata["tool_call_id"])
            if tool_result is not None and tool_result.metadata.get("tool_call_id")
            else None
        ),
        tool_calls=tool_calls,
        reasoning_content=(
            str(text_part.metadata["reasoning_content"])
            if text_part is not None and text_part.metadata.get("reasoning_content")
            else None
        ),
    )


def _agent_message_from_payload(payload: dict[str, Any]) -> Message:
    role = str(payload.get("role") or "")
    if role not in {"system", "user", "assistant", "tool"}:
        raise ValueError(f"unsupported checkpoint message role: {role}")
    content = payload.get("content", "")
    if not isinstance(content, (str, list)):
        raise TypeError("checkpoint message content must be text or structured content")
    return Message(
        role=cast(Role, role),
        content=content,
        name=(str(payload["name"]) if payload.get("name") is not None else None),
        tool_call_id=(
            str(payload["tool_call_id"]) if payload.get("tool_call_id") is not None else None
        ),
        tool_calls=[dict(item) for item in payload.get("tool_calls", []) if isinstance(item, dict)],
        reasoning_content=(
            str(payload["reasoning_content"])
            if payload.get("reasoning_content") is not None
            else None
        ),
    )


def _agent_message_dict(message: SessionMessage) -> dict[str, Any]:
    converted = _agent_message(message)
    return {
        "role": converted.role,
        "content": converted.content,
        "name": converted.name,
        "tool_call_id": converted.tool_call_id,
        "tool_calls": converted.tool_calls,
        "reasoning_content": converted.reasoning_content,
    }
