from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from paicli.session.errors import SessionLeaseConflictError
from paicli.session.models import SessionLease, SessionMessage, SessionRecord, ToolActionSpec
from paicli.session.repository import SessionRepository
from paicli.types import Message, Role


def default_session_database_path() -> Path:
    return Path.home() / ".paicli" / "sessions" / "sessions.db"


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
        self._owner_id = f"interactive_{uuid4().hex}"
        self.record, self._lease = self._open_session(session_id)
        self._active_turn_id = self._find_active_turn_id()

    @property
    def id(self) -> str:
        return self.record.id

    @property
    def lease_token(self) -> str:
        return self._lease.token

    @property
    def session_history(self) -> tuple[SessionMessage, ...]:
        return self.repository.rebuild_session_view(self.id).session_history

    @property
    def agent_history(self) -> list[Message]:
        return [
            _agent_message(message)
            for message in self.repository.rebuild_session_view(self.id).model_messages
        ]

    def restore_agent_history(self, agent: Any) -> None:
        agent.replace_history(self.agent_history)

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
                    lease_token=self._lease.token,
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
                    lease_token=self._lease.token,
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
        actions: list[dict[str, Any]],
    ) -> None:
        turn_id = self._require_active_turn()
        self.repository.prepare_tool_actions(
            self.id,
            turn_id=turn_id,
            model_turn=model_turn,
            assistant_content=assistant_content,
            reasoning_content=reasoning_content,
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
            lease_token=self._lease.token,
        )

    def start_tool_action(self, tool_call_id: str) -> None:
        self.repository.start_tool_action(
            self.id,
            tool_call_id,
            lease_token=self._lease.token,
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
            lease_token=self._lease.token,
        )

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
            lease_token=self._lease.token,
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
            lease_token=self._lease.token,
        )

    def begin_turn(self, message: str) -> str:
        if self._active_turn_id is not None:
            raise RuntimeError("session already has an active turn")
        turn_id = f"turn_{uuid4().hex}"
        self.repository.begin_turn(
            self.id,
            turn_id=turn_id,
            user_content=message,
            lease_token=self._lease.token,
        )
        self._active_turn_id = turn_id
        return turn_id

    def complete_turn(self, assistant_text: str) -> None:
        turn_id = self._require_active_turn()
        self.repository.complete_turn(
            self.id,
            turn_id=turn_id,
            assistant_content=assistant_text,
            lease_token=self._lease.token,
        )
        self._active_turn_id = None

    def interrupt_turn(self, assistant_text: str, *, reason: str) -> None:
        if self._active_turn_id is None:
            return
        turn_id = self._active_turn_id
        self.repository.interrupt_turn(
            self.id,
            turn_id=turn_id,
            assistant_content=assistant_text,
            reason=reason,
            lease_token=self._lease.token,
        )
        self._active_turn_id = None

    def reset_context(self) -> None:
        if self._active_turn_id is not None:
            raise RuntimeError("cannot reset context during an active turn")
        self.repository.reset_context(self.id, lease_token=self._lease.token)

    def list_sessions(self) -> tuple[SessionRecord, ...]:
        return tuple(
            record
            for record in self.repository.list_sessions(
                include_archived=True,
                include_deleted=True,
            )
            if record.workspace_root == self.workspace_root
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
            next_lease = self.repository.acquire_session_lease(
                record.id,
                owner_id=self._owner_id,
            )
            try:
                record = self.repository.unarchive_session(
                    session_id,
                    lease_token=next_lease.token,
                )
            except Exception:
                self.repository.release_session_lease(record.id, next_lease.token)
                raise
            self._switch_to(record, next_lease=next_lease)
        else:
            self._switch_to(record)
        return self.record

    def fork_session(self, *, title: str | None = None) -> SessionRecord:
        self._require_idle()
        record = self.repository.fork_session(
            self.id,
            workspace_root=self.workspace_root,
            title=title,
            lease_token=self._lease.token,
        )
        self._switch_to(record)
        return self.record

    def archive_session(self) -> tuple[SessionRecord, SessionRecord]:
        self._require_idle()
        archived = self.repository.archive_session(
            self.id,
            lease_token=self._lease.token,
        )
        replacement = self.repository.create_session(self.workspace_root)
        self._switch_to(replacement)
        return archived, replacement

    def delete_session(self) -> tuple[SessionRecord, SessionRecord]:
        self._require_idle()
        deleted = self.repository.delete_session(
            self.id,
            lease_token=self._lease.token,
        )
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
        next_lease = self.repository.acquire_session_lease(
            record.id,
            owner_id=self._owner_id,
        )
        try:
            restored = self.repository.restore_session(
                session_id,
                lease_token=next_lease.token,
            )
        except Exception:
            self.repository.release_session_lease(record.id, next_lease.token)
            raise
        self._switch_to(restored, next_lease=next_lease)
        return self.record

    def _request_refreshed_lease(
        self,
        session_id: str,
        lease_token: str,
        *,
        lock_timeout_seconds: float,
    ) -> SessionLease:
        try:
            return self.repository.refresh_session_lease(
                session_id,
                lease_token,
                lock_timeout_seconds=lock_timeout_seconds,
            )
        except SessionLeaseConflictError:
            return self.repository.acquire_session_lease(
                session_id,
                owner_id=self._owner_id,
                lock_timeout_seconds=lock_timeout_seconds,
            )

    def refresh_lease(self) -> None:
        session_id = self.id
        lease_token = self._lease.token
        next_lease = self._request_refreshed_lease(
            session_id,
            lease_token,
            lock_timeout_seconds=5.0,
        )
        if self.id == session_id and self._lease.token == lease_token:
            self._lease = next_lease

    async def refresh_lease_async(
        self,
        *,
        retry_delays: tuple[float, ...] = (0.2, 0.5, 1.0),
        lock_timeout_seconds: float = 0.2,
    ) -> bool:
        """Refresh the Session lease without blocking an async caller's event loop."""
        if lock_timeout_seconds < 0:
            raise ValueError("lock_timeout_seconds must be non-negative")
        if any(delay < 0 for delay in retry_delays):
            raise ValueError("retry delays must be non-negative")

        session_id = self.id
        lease_token = self._lease.token
        next_lease: SessionLease | None = None
        for attempt in range(len(retry_delays) + 1):
            try:
                refresh_task = asyncio.create_task(
                    asyncio.to_thread(
                        self._request_refreshed_lease,
                        session_id,
                        lease_token,
                        lock_timeout_seconds=lock_timeout_seconds,
                    )
                )
                try:
                    next_lease = await asyncio.shield(refresh_task)
                except asyncio.CancelledError:
                    await asyncio.gather(refresh_task, return_exceptions=True)
                    raise
                break
            except sqlite3.OperationalError as exc:
                is_locked = "locked" in str(exc).lower()
                if not is_locked or attempt >= len(retry_delays):
                    raise
                await asyncio.sleep(retry_delays[attempt])

        if next_lease is None:  # pragma: no cover - loop either returns a lease or raises
            return False
        if self.id != session_id or self._lease.token != lease_token:
            if next_lease.token != lease_token:
                await asyncio.to_thread(
                    self.repository.release_session_lease,
                    session_id,
                    next_lease.token,
                )
            return False
        self._lease = next_lease
        return True

    def close(self) -> None:
        self.repository.release_session_lease(self.id, self._lease.token)

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
    ) -> tuple[SessionRecord, SessionLease]:
        if session_id is not None:
            record = self._resolve_workspace_session(session_id)
            return record, self.repository.acquire_session_lease(
                record.id,
                owner_id=self._owner_id,
            )
        for record in self.repository.list_sessions():
            if record.workspace_root != self.workspace_root or record.status == "corrupt":
                continue
            try:
                lease = self.repository.acquire_session_lease(
                    record.id,
                    owner_id=self._owner_id,
                )
            except SessionLeaseConflictError:
                continue
            return record, lease
        record = self.repository.create_session(self.workspace_root)
        return record, self.repository.acquire_session_lease(
            record.id,
            owner_id=self._owner_id,
        )

    def _switch_to(
        self,
        record: SessionRecord,
        *,
        next_lease: SessionLease | None = None,
    ) -> None:
        if record.id == self.id:
            self._lease = next_lease or self.repository.acquire_session_lease(
                record.id,
                owner_id=self._owner_id,
            )
            return
        acquired_lease = next_lease or self.repository.acquire_session_lease(
            record.id, owner_id=self._owner_id
        )
        previous_id = self.id
        previous_token = self._lease.token
        self.record = record
        self._lease = acquired_lease
        self._active_turn_id = self._find_active_turn_id()
        self.repository.release_session_lease(previous_id, previous_token)

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
