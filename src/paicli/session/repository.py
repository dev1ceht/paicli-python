from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from paicli.clock import (
    east_eight_now,
    format_timestamp,
    normalize_optional_timestamp,
    normalize_timestamp,
    now_timestamp,
)
from paicli.session.blob_store import BlobStore
from paicli.session.errors import (
    SessionCorruptError,
    SessionIdempotencyConflictError,
    SessionReadOnlyError,
)
from paicli.session.integrity import (
    EventHashMaterial,
    blob_refs_from_json,
    blob_refs_json,
    canonical_json,
)
from paicli.session.models import (
    BlobReference,
    PendingAction,
    SessionEvent,
    SessionLease,
    SessionMessage,
    SessionRecord,
    SessionRelationship,
    SessionView,
    StoredBlob,
    ToolActionSpec,
)
from paicli.session.operational import PendingActionStore, SessionLeaseStore
from paicli.session.replay import message_from_event, rebuild_session_view
from paicli.session.schema import connect, ensure_schema
from paicli.session.validation import validate_event_payload
from paicli.session.verification import SessionIntegrityVerifier
from paicli.session.versions import EVENT_SCHEMA_VERSION, upcast_event_payload
from paicli.usage import UsageRecord

INLINE_CONTENT_LIMIT_BYTES = 64 * 1024
RESERVED_PUBLIC_EVENT_TYPES = {
    "session.archived",
    "session.created",
    "session.deleted",
    "session.forked",
    "session.metadata_updated",
    "session.restored",
    "session.unarchived",
}
FORK_BOUNDARY_EVENT_TYPES = {"turn.completed", "turn.interrupted", "context.reset"}


@dataclass(frozen=True, slots=True)
class _PreparedMessage:
    event_type: str
    payload: dict[str, Any]
    blob_refs: tuple[BlobReference, ...]


class SessionRepository:
    """SQLite-backed authoritative store for durable sessions."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()
        self._blob_store = BlobStore(self.db_path)
        self._integrity_verifier = SessionIntegrityVerifier(self.db_path)

    def create_session(
        self,
        workspace_root: str | Path,
        *,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionRecord:
        canonical_workspace = str(Path(workspace_root).expanduser().resolve())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session_id = self._create_session_in_transaction(
                connection,
                workspace_root=canonical_workspace,
                title=title,
                metadata=metadata,
            )
        record = self.get_session(session_id)
        if record is None:  # pragma: no cover - the transaction above guarantees the row
            raise RuntimeError(f"created session disappeared: {session_id}")
        return record

    def get_or_create_root_session(
        self,
        workspace_root: str | Path,
        *,
        root_kind: str,
        title: str,
    ) -> SessionRecord:
        """Resolve one normalized root per workspace and kind without a startup race."""
        canonical_workspace = str(Path(workspace_root).expanduser().resolve())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                select session_id
                from session_roots
                where workspace_root = ? and root_kind = ?
                """,
                (canonical_workspace, root_kind),
            ).fetchone()
            if row is None:
                session_id = self._create_session_in_transaction(
                    connection,
                    workspace_root=canonical_workspace,
                    title=title,
                    metadata={"session_kind": root_kind},
                )
                connection.execute(
                    """
                    insert into session_roots(workspace_root, root_kind, session_id)
                    values (?, ?, ?)
                    """,
                    (canonical_workspace, root_kind, session_id),
                )
            else:
                session_id = str(row["session_id"])
        record = self.get_session(session_id)
        if record is None:  # pragma: no cover
            raise RuntimeError(f"root session disappeared: {session_id}")
        return record

    def create_child_session(
        self,
        parent_session_id: str,
        *,
        relation_type: str,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
        lease_token: str | None = None,
    ) -> SessionRecord:
        if not relation_type:
            raise ValueError("child session relation_type must be non-empty")
        now = _now()
        relationship_metadata = dict(metadata or {})
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            child_session_id = self._create_child_session_in_transaction(
                connection,
                parent_session_id=parent_session_id,
                relation_type=relation_type,
                title=title,
                metadata=metadata,
                created_at=now,
                relationship_metadata=relationship_metadata,
                lease_token=lease_token,
            )
        child = self.get_session(child_session_id)
        if child is None:  # pragma: no cover
            raise RuntimeError(f"created child session disappeared: {child_session_id}")
        return child

    def create_background_task(
        self,
        parent_session_id: str,
        *,
        queue_session_id: str,
        task_id: str,
        prompt: str,
        retry_of: str | None,
        relation_type: str,
        lease_token: str | None = None,
    ) -> SessionRecord:
        """Create the task child, queue projection, and lifecycle fact atomically."""
        now = _now()
        metadata = {"session_kind": "background_task", "task_id": task_id}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            child_session_id = self._create_child_session_in_transaction(
                connection,
                parent_session_id=parent_session_id,
                relation_type=relation_type,
                title=prompt[:80] or task_id,
                metadata=metadata,
                relationship_metadata=metadata,
                created_at=now,
                lease_token=lease_token,
            )
            connection.execute(
                """
                insert into background_tasks(
                    id, session_id, parent_session_id, queue_session_id, prompt, status,
                    created_at, updated_at, retry_of
                )
                values (?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    task_id,
                    child_session_id,
                    parent_session_id,
                    queue_session_id,
                    prompt,
                    now,
                    now,
                    retry_of,
                ),
            )
            self._append_event_in_transaction(
                connection,
                session_id=child_session_id,
                event_type="background_task.queued",
                payload={"task_id": task_id, "prompt": prompt, "retry_of": retry_of},
                created_at=now,
            )
        child = self.get_session(child_session_id)
        if child is None:  # pragma: no cover
            raise RuntimeError(f"created task session disappeared: {child_session_id}")
        return child

    def claim_next_background_task(
        self,
        queue_session_id: str,
        *,
        owner_id: str,
        ttl_seconds: int = 60,
    ) -> dict[str, Any] | None:
        """Claim one queued task and append its running fact in the same transaction."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                select id, prompt, status, created_at, updated_at, started_at, finished_at,
                       result, error, retry_of, session_id, parent_session_id
                from background_tasks
                where queue_session_id = ? and status = 'queued'
                order by created_at, rowid
                limit 1
                """,
                (queue_session_id,),
            ).fetchone()
            if row is None:
                return None
            updated_at = _now()
            claim_token = f"task_claim_{uuid4().hex}"
            claim_expires_at = format_timestamp(east_eight_now() + timedelta(seconds=ttl_seconds))
            updated = connection.execute(
                """
                update background_tasks
                set status = 'running', updated_at = ?, started_at = coalesce(started_at, ?),
                    claim_owner = ?, claim_token = ?, claim_expires_at = ?
                where id = ? and status = 'queued'
                """,
                (
                    updated_at,
                    updated_at,
                    owner_id,
                    claim_token,
                    claim_expires_at,
                    row["id"],
                ),
            )
            if updated.rowcount != 1:
                return None
            self._append_event_in_transaction(
                connection,
                session_id=str(row["session_id"]),
                event_type="background_task.running",
                payload={"task_id": str(row["id"])},
                created_at=updated_at,
            )
            claimed = dict(row)
            claimed["status"] = "running"
            claimed["updated_at"] = updated_at
            claimed["started_at"] = row["started_at"] or updated_at
            claimed["claim_token"] = claim_token
            return claimed

    def refresh_background_task_claim(
        self,
        task_id: str,
        *,
        owner_id: str,
        claim_token: str,
        ttl_seconds: int = 60,
    ) -> bool:
        now = _now()
        expires_at = format_timestamp(east_eight_now() + timedelta(seconds=ttl_seconds))
        with self._connect() as connection:
            updated = connection.execute(
                """
                update background_tasks
                set claim_expires_at = ?, updated_at = ?
                where id = ? and status = 'running'
                  and claim_owner = ? and claim_token = ?
                  and claim_expires_at > ?
                """,
                (expires_at, now, task_id, owner_id, claim_token, now),
            )
        return updated.rowcount == 1

    def pause_background_task_for_approval(
        self,
        task_id: str,
        *,
        session_id: str,
        tool_call_id: str,
        checkpoint: dict[str, Any],
        approval_id: str,
        request: dict[str, Any],
        claim_owner: str,
        claim_token: str,
        invalidation_reason: str | None = None,
        lease_token: str | None = None,
    ) -> bool:
        """Atomically pause both the Agent action and its background task."""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_writable_session(
                connection,
                session_id,
                lease_token=lease_token,
            )
            action = PendingActionStore.get(connection, session_id, tool_call_id)
            if action is None:
                raise KeyError(f"pending tool action not found: {tool_call_id}")
            if action.status in {"completed", "abandoned"}:
                raise ValueError(f"tool action is already settled: {tool_call_id}")
            task_update = connection.execute(
                """
                update background_tasks
                set status = 'waiting_approval', updated_at = ?
                where id = ? and session_id = ? and status = 'running'
                  and claim_owner = ? and claim_token = ? and claim_expires_at > ?
                """,
                (now, task_id, session_id, claim_owner, claim_token, now),
            )
            if task_update.rowcount != 1:
                return False
            requested_key = f"{action.turn_id}:approval:{tool_call_id}:requested"
            if not self._event_idempotency_key_exists(
                connection,
                session_id,
                requested_key,
            ):
                self._append_event_in_transaction(
                    connection,
                    session_id=session_id,
                    event_type="approval.requested",
                    payload={
                        "tool_call_id": tool_call_id,
                        "tool_name": action.tool_name,
                    },
                    turn_id=action.turn_id,
                    idempotency_key=requested_key,
                )
            if not PendingActionStore.transition(
                connection,
                session_id,
                tool_call_id,
                status="waiting_approval",
                approval_status="requested",
                now=now,
                expected_statuses=("prepared", "executing", "waiting_approval"),
            ):
                raise ValueError(f"tool action is already settled: {tool_call_id}")
            if invalidation_reason:
                connection.execute(
                    """
                    insert into task_approvals(
                        id, task_id, status, request_json, requested_at,
                        decided_at, decision_source
                    ) values (?, ?, 'invalidated', ?, ?, ?, ?)
                    """,
                    (
                        f"approval_{uuid4().hex}",
                        task_id,
                        _json_dumps(request),
                        now,
                        now,
                        invalidation_reason,
                    ),
                )
            connection.execute(
                """
                insert into task_checkpoints(
                    task_id, schema_version, state_json, created_at, updated_at
                )
                values (?, 'approval-v1', ?, ?, ?)
                on conflict(task_id) do update set
                    schema_version = excluded.schema_version,
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (task_id, _json_dumps(checkpoint), now, now),
            )
            connection.execute(
                """
                insert into task_approvals(
                    id, task_id, status, request_json, requested_at
                ) values (?, ?, 'requested', ?, ?)
                """,
                (approval_id, task_id, _json_dumps(request), now),
            )
            return True

    def transition_background_task(
        self,
        task_id: str,
        *,
        status: str,
        from_status: str,
        result: str | None,
        error: str | None,
        claim_owner: str,
        claim_token: str,
    ) -> bool:
        """Commit a terminal task projection and its Session fact together."""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "select session_id from background_tasks where id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return False
            updated = connection.execute(
                """
                update background_tasks
                set status = ?, result = ?, error = ?, updated_at = ?, finished_at = ?,
                    claim_owner = null, claim_token = null, claim_expires_at = null
                where id = ? and status = ?
                  and claim_owner = ? and claim_token = ? and claim_expires_at > ?
                """,
                (
                    status,
                    result,
                    error,
                    now,
                    now,
                    task_id,
                    from_status,
                    claim_owner,
                    claim_token,
                    now,
                ),
            )
            if updated.rowcount != 1:
                return False
            payload: dict[str, str] = {"task_id": task_id}
            if result is not None:
                payload["result"] = result
            if error is not None:
                payload["error"] = error
            self._append_event_in_transaction(
                connection,
                session_id=str(row["session_id"]),
                event_type=f"background_task.{status}",
                payload=payload,
                created_at=now,
            )
            return True

    def cancel_background_task(self, task_id: str, *, queue_session_id: str) -> bool:
        """Cancel a task and its outstanding approval as one durable transition."""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                select session_id
                from background_tasks
                where id = ? and queue_session_id = ?
                """,
                (task_id, queue_session_id),
            ).fetchone()
            if row is None:
                return False
            updated = connection.execute(
                """
                update background_tasks
                set status = 'canceled', updated_at = ?, finished_at = ?,
                    claim_owner = null, claim_token = null, claim_expires_at = null
                where id = ? and status in ('queued', 'running', 'waiting_approval')
                """,
                (now, now, task_id),
            )
            if updated.rowcount != 1:
                return False
            active_turn_id = self._find_active_turn_in_transaction(
                connection,
                str(row["session_id"]),
            )
            if active_turn_id is not None:
                self._interrupt_turn_in_transaction(
                    connection,
                    session_id=str(row["session_id"]),
                    turn_id=active_turn_id,
                    reason="background_task_canceled",
                )
            connection.execute(
                """
                update task_approvals
                set status = 'canceled', decided_at = ?, decision_source = 'cancel'
                where task_id = ? and status = 'requested'
                """,
                (now, task_id),
            )
            self._append_event_in_transaction(
                connection,
                session_id=str(row["session_id"]),
                event_type="background_task.canceled",
                payload={"task_id": task_id},
                created_at=now,
            )
            return True

    def fail_interrupted_background_tasks(
        self,
        queue_session_id: str,
        error: str,
    ) -> int:
        """Close active Agent turns and fail Runtime tasks left running after shutdown."""
        now = _now()
        failed = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                select id, session_id
                from background_tasks
                where queue_session_id = ? and status = 'running'
                  and (claim_expires_at is null or claim_expires_at <= ?)
                """,
                (queue_session_id, now),
            ).fetchall()
            for row in rows:
                task_id = str(row["id"])
                session_id = str(row["session_id"])
                active_turn_id = self._find_active_turn_in_transaction(
                    connection,
                    session_id,
                )
                if active_turn_id is not None:
                    self._interrupt_turn_in_transaction(
                        connection,
                        session_id=session_id,
                        turn_id=active_turn_id,
                        reason="background_task_process_restarted",
                    )
                updated = connection.execute(
                    """
                    update background_tasks
                    set status = 'failed', result = null, error = ?,
                        updated_at = ?, finished_at = ?,
                        claim_owner = null, claim_token = null, claim_expires_at = null
                    where id = ? and status = 'running'
                    """,
                    (error, now, now, task_id),
                )
                if updated.rowcount != 1:
                    continue
                self._append_event_in_transaction(
                    connection,
                    session_id=session_id,
                    event_type="background_task.failed",
                    payload={"task_id": task_id, "error": error},
                    created_at=now,
                )
                failed += 1
        return failed

    @staticmethod
    def _find_active_turn_in_transaction(
        connection: sqlite3.Connection,
        session_id: str,
    ) -> str | None:
        active_turn_id: str | None = None
        rows = connection.execute(
            """
            select type, turn_id
            from session_events
            where session_id = ?
              and type in ('turn.started', 'turn.completed', 'turn.interrupted')
            order by sequence
            """,
            (session_id,),
        ).fetchall()
        for row in rows:
            event_type = str(row["type"])
            turn_id = str(row["turn_id"]) if row["turn_id"] is not None else None
            if event_type == "turn.started":
                active_turn_id = turn_id
            elif turn_id == active_turn_id:
                active_turn_id = None
        return active_turn_id

    def get_parent_relationship(self, child_session_id: str) -> SessionRelationship | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                select parent_session_id, child_session_id, relation_type,
                       created_at, metadata_json
                from session_relationships
                where child_session_id = ?
                """,
                (child_session_id,),
            ).fetchone()
        return _relationship_from_row(row) if row is not None else None

    def list_child_sessions(
        self,
        parent_session_id: str,
        *,
        relation_type: str | None = None,
    ) -> list[SessionRecord]:
        condition = "and r.relation_type = ?" if relation_type is not None else ""
        values: tuple[Any, ...] = (
            (parent_session_id, relation_type)
            if relation_type is not None
            else (parent_session_id,)
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                select s.*
                from session_relationships r
                join sessions s on s.id = r.child_session_id
                where r.parent_session_id = ? {condition}
                order by r.created_at, r.child_session_id
                """,
                values,
            ).fetchall()
        return [_session_from_row(row) for row in rows]

    def acquire_session_lease(
        self,
        session_id: str,
        *,
        owner_id: str,
        ttl_seconds: int = 60,
        lock_timeout_seconds: float = 5.0,
    ) -> SessionLease:
        if not owner_id:
            raise ValueError("lease owner_id must be non-empty")
        if ttl_seconds <= 0:
            raise ValueError("lease ttl_seconds must be positive")
        with self._connect(timeout_seconds=lock_timeout_seconds) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_leaseable_session(
                connection,
                session_id,
                enforce_lease=False,
            )
            return SessionLeaseStore.acquire(
                connection,
                session_id,
                owner_id=owner_id,
                ttl_seconds=ttl_seconds,
            )

    def refresh_session_lease(
        self,
        session_id: str,
        token: str,
        *,
        ttl_seconds: int = 60,
        lock_timeout_seconds: float = 5.0,
    ) -> SessionLease:
        if ttl_seconds <= 0:
            raise ValueError("lease ttl_seconds must be positive")
        with self._connect(timeout_seconds=lock_timeout_seconds) as connection:
            connection.execute("BEGIN IMMEDIATE")
            return SessionLeaseStore.refresh(
                connection,
                session_id,
                token,
                ttl_seconds=ttl_seconds,
            )

    def release_session_lease(self, session_id: str, token: str) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return SessionLeaseStore.release(connection, session_id, token)

    def update_session_metadata(
        self,
        session_id: str,
        *,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
        lease_token: str | None = None,
    ) -> SessionRecord:
        metadata_patch = _event_metadata(metadata or {}, preserve_none=True)
        event_payload: dict[str, Any] = dict(metadata_patch)
        if title is not None:
            event_payload["title"] = title
        if not event_payload:
            return self._require_session_record(session_id)

        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_writable_session(
                connection,
                session_id,
                lease_token=lease_token,
            )
            row = connection.execute(
                "select title, metadata_json from sessions where id = ?",
                (session_id,),
            ).fetchone()
            current_metadata = json.loads(row["metadata_json"])
            for key, value in metadata_patch.items():
                if value is None:
                    current_metadata.pop(key, None)
                else:
                    current_metadata[key] = value
            self._append_event_in_transaction(
                connection,
                session_id=session_id,
                event_type="session.metadata_updated",
                payload=event_payload,
                created_at=now,
            )
            connection.execute(
                """
                update sessions
                set title = ?, metadata_json = ?
                where id = ?
                """,
                (
                    title if title is not None else str(row["title"]),
                    _json_dumps(current_metadata),
                    session_id,
                ),
            )
        return self._require_session_record(session_id)

    def append_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        turn_id: str | None = None,
        idempotency_key: str | None = None,
        blob_refs: list[BlobReference] | tuple[BlobReference, ...] = (),
        lease_token: str | None = None,
    ) -> SessionEvent:
        if event_type in RESERVED_PUBLIC_EVENT_TYPES:
            raise ValueError(
                f"reserved event type must use its transactional repository method: {event_type}"
            )
        payload_json = _json_dumps(payload)
        normalized_blob_refs = tuple(blob_refs)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_writable_session(connection, session_id, lease_token=lease_token)
            if idempotency_key is not None:
                row = connection.execute(
                    """
                    select id, session_id, sequence, type, payload_json, schema_version,
                           created_at, previous_event_hash, event_hash, turn_id,
                           idempotency_key, source_session_id, source_event_id,
                           blob_refs_json
                    from session_events
                    where session_id = ? and idempotency_key = ?
                    """,
                    (session_id, idempotency_key),
                ).fetchone()
                if row is not None:
                    existing = _event_from_row(row)
                    if (
                        existing.type != event_type
                        or _json_dumps(existing.payload) != payload_json
                        or existing.turn_id != turn_id
                        or existing.blob_refs != normalized_blob_refs
                    ):
                        raise SessionIdempotencyConflictError(
                            f"idempotency key already used by another event: {idempotency_key}"
                        )
                    return existing
            return self._append_event_in_transaction(
                connection,
                session_id=session_id,
                event_type=event_type,
                payload=payload,
                turn_id=turn_id,
                idempotency_key=idempotency_key,
                blob_refs=normalized_blob_refs,
            )

    def append_message(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        partial: bool = False,
        interruption_reason: str | None = None,
        turn_id: str | None = None,
        idempotency_key: str | None = None,
        lease_token: str | None = None,
    ) -> SessionMessage:
        prepared = self._prepare_message(
            session_id,
            role=role,
            content=content,
            partial=partial,
            interruption_reason=interruption_reason,
            idempotency_key=idempotency_key,
        )
        event = self.append_event(
            session_id,
            prepared.event_type,
            prepared.payload,
            turn_id=turn_id,
            idempotency_key=idempotency_key,
            blob_refs=prepared.blob_refs,
            lease_token=lease_token,
        )
        return message_from_event(event, blob_loader=self._load_blob_bytes)

    def prepare_tool_actions(
        self,
        session_id: str,
        *,
        turn_id: str,
        model_turn: int,
        assistant_content: str,
        reasoning_content: str | None = None,
        actions: tuple[ToolActionSpec, ...],
        lease_token: str | None = None,
    ) -> tuple[PendingAction, ...]:
        if not actions:
            raise ValueError("tool action batch must not be empty")
        if len({action.tool_call_id for action in actions}) != len(actions):
            raise ValueError("tool action ids must be unique within a batch")
        for action in actions:
            if not action.tool_call_id or not action.tool_name:
                raise ValueError("tool actions require a call id and name")
        message_key = f"{turn_id}:assistant-step:{model_turn}"
        prepared_message = self._prepare_message(
            session_id,
            role="assistant",
            content=assistant_content,
            idempotency_key=message_key,
        )
        if reasoning_content:
            prepared_message.payload["parts"][0]["metadata"]["reasoning_content"] = (
                reasoning_content
            )
        prepared_message.payload["parts"].extend(
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
            for action in actions
        )
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_writable_session(
                connection,
                session_id,
                lease_token=lease_token,
            )
            self._append_event_in_transaction(
                connection,
                session_id=session_id,
                event_type=prepared_message.event_type,
                payload=prepared_message.payload,
                turn_id=turn_id,
                idempotency_key=message_key,
                blob_refs=prepared_message.blob_refs,
            )
            for batch_index, action in enumerate(actions):
                payload = {
                    "tool_call_id": action.tool_call_id,
                    "tool_name": action.tool_name,
                    "arguments": action.arguments,
                    "raw_call": action.raw_call,
                    "is_read_only": action.is_read_only,
                    "is_idempotent": action.is_idempotent,
                    "model_turn": model_turn,
                    "batch_index": batch_index,
                    "status": "prepared",
                }
                self._append_event_in_transaction(
                    connection,
                    session_id=session_id,
                    event_type="pending_action.prepared",
                    payload=payload,
                    turn_id=turn_id,
                    idempotency_key=f"{turn_id}:action:{action.tool_call_id}:prepared",
                )
                PendingActionStore.insert_prepared(
                    connection,
                    session_id=session_id,
                    turn_id=turn_id,
                    model_turn=model_turn,
                    batch_index=batch_index,
                    action=action,
                    now=now,
                )
        return tuple(
            action
            for action in self.list_pending_actions(session_id)
            if action.tool_call_id in {spec.tool_call_id for spec in actions}
        )

    def list_pending_actions(
        self,
        session_id: str,
        *,
        include_settled: bool = False,
    ) -> list[PendingAction]:
        with self._connect() as connection:
            return PendingActionStore.list(
                connection,
                session_id,
                include_settled=include_settled,
            )

    def start_tool_action(
        self,
        session_id: str,
        tool_call_id: str,
        *,
        lease_token: str | None = None,
    ) -> PendingAction:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_writable_session(
                connection,
                session_id,
                lease_token=lease_token,
            )
            row = connection.execute(
                """
                select turn_id, status
                from pending_actions
                where session_id = ? and tool_call_id = ?
                """,
                (session_id, tool_call_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"pending tool action not found: {tool_call_id}")
            if str(row["status"]) in {"completed", "abandoned"}:
                raise ValueError(f"tool action is already settled: {tool_call_id}")
            if str(row["status"]) != "executing":
                started_key = f"{row['turn_id']}:action:{tool_call_id}:started"
                if not self._event_idempotency_key_exists(
                    connection,
                    session_id,
                    started_key,
                ):
                    self._append_event_in_transaction(
                        connection,
                        session_id=session_id,
                        event_type="pending_action.started",
                        payload={"tool_call_id": tool_call_id},
                        turn_id=str(row["turn_id"]),
                        idempotency_key=started_key,
                    )
                PendingActionStore.set_status(
                    connection,
                    session_id,
                    tool_call_id,
                    status="executing",
                    now=now,
                    expected_statuses=("prepared", "waiting_approval"),
                )
        return self._require_pending_action(session_id, tool_call_id)

    def request_tool_approval(
        self,
        session_id: str,
        tool_call_id: str,
        *,
        lease_token: str | None = None,
    ) -> PendingAction:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_writable_session(
                connection,
                session_id,
                lease_token=lease_token,
            )
            action = PendingActionStore.get(connection, session_id, tool_call_id)
            if action is None:
                raise KeyError(f"pending tool action not found: {tool_call_id}")
            if action.status in {"completed", "abandoned"}:
                raise ValueError(f"tool action is already settled: {tool_call_id}")
            requested_key = f"{action.turn_id}:approval:{tool_call_id}:requested"
            if not self._event_idempotency_key_exists(
                connection,
                session_id,
                requested_key,
            ):
                self._append_event_in_transaction(
                    connection,
                    session_id=session_id,
                    event_type="approval.requested",
                    payload={
                        "tool_call_id": tool_call_id,
                        "tool_name": action.tool_name,
                    },
                    turn_id=action.turn_id,
                    idempotency_key=requested_key,
                )
            if not PendingActionStore.transition(
                connection,
                session_id,
                tool_call_id,
                status="waiting_approval",
                approval_status="requested",
                now=now,
                expected_statuses=("prepared", "executing", "waiting_approval"),
            ):
                raise ValueError(f"tool action is already settled: {tool_call_id}")
        return self._require_pending_action(session_id, tool_call_id)

    def resolve_tool_approval(
        self,
        session_id: str,
        tool_call_id: str,
        *,
        decision: str,
        deferred_execution: bool = False,
        lease_token: str | None = None,
    ) -> PendingAction:
        if decision not in {"approve", "allow_session", "deny", "skip"}:
            raise ValueError(f"unsupported tool approval decision: {decision}")
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_writable_session(
                connection,
                session_id,
                lease_token=lease_token,
            )
            action = PendingActionStore.get(connection, session_id, tool_call_id)
            if action is None:
                raise KeyError(f"pending tool action not found: {tool_call_id}")
            if action.status in {"completed", "abandoned"}:
                raise ValueError(f"tool action is already settled: {tool_call_id}")
            if action.approval_status != "requested":
                raise ValueError(f"tool action is not waiting for approval: {tool_call_id}")
            self._append_event_in_transaction(
                connection,
                session_id=session_id,
                event_type="approval.resolved",
                payload={
                    "tool_call_id": tool_call_id,
                    "decision": decision,
                },
                turn_id=action.turn_id,
                idempotency_key=f"{action.turn_id}:approval:{tool_call_id}:resolved",
            )
            if not PendingActionStore.transition(
                connection,
                session_id,
                tool_call_id,
                status="prepared" if deferred_execution else "executing",
                approval_status=decision,
                now=now,
                expected_statuses=("waiting_approval",),
                expected_approval="requested",
            ):
                raise ValueError(f"tool action approval changed concurrently: {tool_call_id}")
        return self._require_pending_action(session_id, tool_call_id)

    def complete_tool_action(
        self,
        session_id: str,
        tool_call_id: str,
        *,
        content: str,
        is_error: bool,
        lease_token: str | None = None,
    ) -> SessionMessage:
        row = self._require_pending_action(session_id, tool_call_id)
        message_key = f"{row.turn_id}:tool-result:{tool_call_id}"
        prepared = self._prepare_message(
            session_id,
            role="tool",
            content=content,
            idempotency_key=message_key,
        )
        prepared.payload["parts"][0]["kind"] = "tool_result"
        prepared.payload["parts"][0]["metadata"].update(
            {
                "tool_call_id": tool_call_id,
                "tool_name": row.tool_name,
                "is_error": is_error,
                "execution_outcome": "error" if is_error else "completed",
            }
        )
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_writable_session(
                connection,
                session_id,
                lease_token=lease_token,
            )
            current = connection.execute(
                """
                select status
                from pending_actions
                where session_id = ? and tool_call_id = ?
                """,
                (session_id, tool_call_id),
            ).fetchone()
            if current is None:
                raise KeyError(f"pending tool action not found: {tool_call_id}")
            if str(current["status"]) in {"completed", "abandoned"}:
                raise ValueError(f"tool action is already settled: {tool_call_id}")
            event = self._append_event_in_transaction(
                connection,
                session_id=session_id,
                event_type=prepared.event_type,
                payload=prepared.payload,
                turn_id=row.turn_id,
                idempotency_key=message_key,
                blob_refs=prepared.blob_refs,
            )
            self._append_event_in_transaction(
                connection,
                session_id=session_id,
                event_type="pending_action.completed",
                payload={
                    "tool_call_id": tool_call_id,
                    "is_error": is_error,
                },
                turn_id=row.turn_id,
                idempotency_key=f"{row.turn_id}:action:{tool_call_id}:completed",
            )
            if not PendingActionStore.set_status(
                connection,
                session_id,
                tool_call_id,
                status="completed",
                now=now,
                expected_statuses=("prepared", "executing", "waiting_approval"),
            ):
                raise ValueError(f"tool action is already settled: {tool_call_id}")
        return message_from_event(event, blob_loader=self._load_blob_bytes)

    def abandon_tool_action(
        self,
        session_id: str,
        tool_call_id: str,
        *,
        reason: str,
        lease_token: str | None = None,
    ) -> SessionMessage:
        row = self._require_pending_action(session_id, tool_call_id)
        message_key, prepared = self._prepare_abandoned_action_message(
            session_id,
            row,
            reason=reason,
        )
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_writable_session(
                connection,
                session_id,
                lease_token=lease_token,
            )
            current = connection.execute(
                """
                select status
                from pending_actions
                where session_id = ? and tool_call_id = ?
                """,
                (session_id, tool_call_id),
            ).fetchone()
            if current is None:
                raise KeyError(f"pending tool action not found: {tool_call_id}")
            if str(current["status"]) in {"completed", "abandoned"}:
                raise ValueError(f"tool action is already settled: {tool_call_id}")
            event = self._append_event_in_transaction(
                connection,
                session_id=session_id,
                event_type=prepared.event_type,
                payload=prepared.payload,
                turn_id=row.turn_id,
                idempotency_key=message_key,
                blob_refs=prepared.blob_refs,
            )
            self._append_event_in_transaction(
                connection,
                session_id=session_id,
                event_type="pending_action.abandoned",
                payload={
                    "tool_call_id": tool_call_id,
                    "reason": reason,
                    "execution_outcome": "unknown",
                },
                turn_id=row.turn_id,
                idempotency_key=f"{row.turn_id}:action:{tool_call_id}:abandoned",
            )
            if not PendingActionStore.set_status(
                connection,
                session_id,
                tool_call_id,
                status="abandoned",
                now=now,
                expected_statuses=("prepared", "executing", "waiting_approval"),
            ):
                raise ValueError(f"tool action is already settled: {tool_call_id}")
        return message_from_event(event, blob_loader=self._load_blob_bytes)

    def _prepare_abandoned_action_message(
        self,
        session_id: str,
        action: PendingAction,
        *,
        reason: str,
    ) -> tuple[str, _PreparedMessage]:
        content = (
            f'Tool "{action.tool_name}" execution outcome is unknown after {reason}; '
            "it may not have run, partially run, or completed and must not be "
            "automatically repeated. Inspect workspace state before retrying."
        )
        message_key = f"{action.turn_id}:tool-result:{action.tool_call_id}:unknown"
        prepared = self._prepare_message(
            session_id,
            role="tool",
            content=content,
            idempotency_key=message_key,
        )
        prepared.payload["parts"][0]["kind"] = "tool_result"
        prepared.payload["parts"][0]["metadata"].update(
            {
                "tool_call_id": action.tool_call_id,
                "tool_name": action.tool_name,
                "is_error": True,
                "execution_outcome": "unknown",
                "interruption_reason": reason,
            }
        )
        return message_key, prepared

    def _require_pending_action(self, session_id: str, tool_call_id: str) -> PendingAction:
        with self._connect() as connection:
            action = PendingActionStore.get(connection, session_id, tool_call_id)
        if action is None:
            raise KeyError(f"pending tool action not found: {tool_call_id}")
        return action

    def begin_turn(
        self,
        session_id: str,
        *,
        turn_id: str,
        user_content: str,
        lease_token: str | None = None,
    ) -> SessionMessage:
        prepared = self._prepare_message(
            session_id,
            role="user",
            content=user_content,
            idempotency_key=f"{turn_id}:user",
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_writable_session(
                connection,
                session_id,
                lease_token=lease_token,
            )
            self._append_event_in_transaction(
                connection,
                session_id=session_id,
                event_type="turn.started",
                payload={},
                turn_id=turn_id,
                idempotency_key=f"{turn_id}:started",
            )
            event = self._append_event_in_transaction(
                connection,
                session_id=session_id,
                event_type=prepared.event_type,
                payload=prepared.payload,
                turn_id=turn_id,
                idempotency_key=f"{turn_id}:user",
                blob_refs=prepared.blob_refs,
            )
        return message_from_event(event, blob_loader=self._load_blob_bytes)

    def complete_turn(
        self,
        session_id: str,
        *,
        turn_id: str,
        assistant_content: str,
        context_checkpoint: dict[str, Any] | None = None,
        lease_token: str | None = None,
    ) -> SessionMessage:
        prepared = self._prepare_message(
            session_id,
            role="assistant",
            content=assistant_content,
            idempotency_key=f"{turn_id}:assistant",
        )
        checkpoint_payload: dict[str, Any] | None = None
        checkpoint_blob_refs: tuple[BlobReference, ...] = ()
        if context_checkpoint is not None:
            with self._connect() as checkpoint_connection:
                checkpoint_is_current = self._checkpoint_is_current(
                    checkpoint_connection,
                    session_id,
                    str(context_checkpoint["checkpoint_id"]),
                )
            if checkpoint_is_current:
                context_checkpoint = None
            else:
                context_checkpoint = self._annotate_context_checkpoint_messages(
                    session_id,
                    context_checkpoint,
                    pending_assistant=prepared.payload,
                )
                checkpoint_payload, checkpoint_blob_refs = self._prepare_context_checkpoint(
                    context_checkpoint
                )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_writable_session(
                connection,
                session_id,
                lease_token=lease_token,
            )
            event = self._append_event_in_transaction(
                connection,
                session_id=session_id,
                event_type=prepared.event_type,
                payload=prepared.payload,
                turn_id=turn_id,
                idempotency_key=f"{turn_id}:assistant",
                blob_refs=prepared.blob_refs,
            )
            self._append_event_in_transaction(
                connection,
                session_id=session_id,
                event_type="turn.completed",
                payload={},
                turn_id=turn_id,
                idempotency_key=f"{turn_id}:completed",
            )
            if context_checkpoint is not None and not self._checkpoint_is_current(
                connection,
                session_id,
                str(context_checkpoint["checkpoint_id"]),
            ):
                checkpoint_id = str(context_checkpoint["checkpoint_id"])
                self._append_event_in_transaction(
                    connection,
                    session_id=session_id,
                    event_type="context.compacted",
                    payload={
                        "checkpoint_id": checkpoint_id,
                        "summary": str(context_checkpoint["summary"]),
                        "compaction": dict(context_checkpoint["compaction"]),
                        "pressure": dict(context_checkpoint.get("pressure") or {}),
                        "provider": str(context_checkpoint.get("provider") or ""),
                        "model": str(context_checkpoint.get("model") or ""),
                    },
                    turn_id=turn_id,
                    idempotency_key=f"{turn_id}:context-compacted",
                )
                self._append_event_in_transaction(
                    connection,
                    session_id=session_id,
                    event_type="context.checkpoint_created",
                    payload=checkpoint_payload or {},
                    turn_id=turn_id,
                    idempotency_key=f"{turn_id}:context-checkpoint",
                    blob_refs=checkpoint_blob_refs,
                )
        return message_from_event(event, blob_loader=self._load_blob_bytes)

    def record_usage(
        self,
        session_id: str,
        record: UsageRecord,
        *,
        turn_id: str,
        lease_token: str | None = None,
    ) -> SessionEvent:
        return self.append_event(
            session_id,
            "usage.recorded",
            record.to_payload(),
            turn_id=turn_id,
            idempotency_key=f"{turn_id}:usage:{record.usage_id}",
            lease_token=lease_token,
        )

    @staticmethod
    def _checkpoint_is_current(
        connection: sqlite3.Connection,
        session_id: str,
        checkpoint_id: str,
    ) -> bool:
        row = connection.execute(
            """
            select payload_json
            from session_events
            where session_id = ? and type = 'context.checkpoint_created'
            order by sequence desc
            limit 1
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return False
        payload = json.loads(str(row["payload_json"]))
        return isinstance(payload, dict) and payload.get("checkpoint_id") == checkpoint_id

    def interrupt_turn(
        self,
        session_id: str,
        *,
        turn_id: str,
        assistant_content: str,
        reason: str,
        lease_token: str | None = None,
    ) -> SessionMessage | None:
        prepared = (
            self._prepare_message(
                session_id,
                role="assistant",
                content=assistant_content,
                partial=True,
                interruption_reason=reason,
                idempotency_key=f"{turn_id}:assistant-partial",
            )
            if assistant_content
            else None
        )
        message_event: SessionEvent | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_writable_session(
                connection,
                session_id,
                lease_token=lease_token,
            )
            terminal = connection.execute(
                """
                select 1
                from session_events
                where session_id = ? and turn_id = ?
                  and type in ('turn.completed', 'turn.interrupted')
                limit 1
                """,
                (session_id, turn_id),
            ).fetchone()
            if terminal is not None:
                return None
            message_event = self._interrupt_turn_in_transaction(
                connection,
                session_id=session_id,
                turn_id=turn_id,
                reason=reason,
                prepared_assistant=prepared,
            )
        if message_event is None:
            return None
        return message_from_event(message_event, blob_loader=self._load_blob_bytes)

    def _interrupt_turn_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        turn_id: str,
        reason: str,
        prepared_assistant: _PreparedMessage | None = None,
    ) -> SessionEvent | None:
        message_event: SessionEvent | None = None
        pending_actions = PendingActionStore.list(
            connection,
            session_id,
            include_settled=False,
            turn_id=turn_id,
        )
        for action in pending_actions:
            action_message_key, action_message = self._prepare_abandoned_action_message(
                session_id,
                action,
                reason=reason,
            )
            self._append_event_in_transaction(
                connection,
                session_id=session_id,
                event_type=action_message.event_type,
                payload=action_message.payload,
                turn_id=turn_id,
                idempotency_key=action_message_key,
                blob_refs=action_message.blob_refs,
            )
            self._append_event_in_transaction(
                connection,
                session_id=session_id,
                event_type="pending_action.abandoned",
                payload={
                    "tool_call_id": action.tool_call_id,
                    "reason": reason,
                    "execution_outcome": "unknown",
                },
                turn_id=turn_id,
                idempotency_key=f"{turn_id}:action:{action.tool_call_id}:abandoned",
            )
            PendingActionStore.set_status(
                connection,
                session_id,
                action.tool_call_id,
                status="abandoned",
                now=_now(),
                expected_statuses=("prepared", "executing", "waiting_approval"),
            )
        if prepared_assistant is not None:
            message_event = self._append_event_in_transaction(
                connection,
                session_id=session_id,
                event_type=prepared_assistant.event_type,
                payload=prepared_assistant.payload,
                turn_id=turn_id,
                idempotency_key=f"{turn_id}:assistant-partial",
                blob_refs=prepared_assistant.blob_refs,
            )
        self._append_event_in_transaction(
            connection,
            session_id=session_id,
            event_type="turn.interrupted",
            payload={"reason": reason},
            turn_id=turn_id,
            idempotency_key=f"{turn_id}:interrupted",
        )
        return message_event

    def _prepare_message(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        partial: bool = False,
        interruption_reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> _PreparedMessage:
        if role not in {"user", "assistant", "tool"}:
            raise ValueError(f"unsupported message role: {role}")
        if partial and role != "assistant":
            raise ValueError("only assistant messages can be partial")
        if idempotency_key is not None:
            message_id = f"msg_{uuid5(NAMESPACE_URL, f'{session_id}:{idempotency_key}').hex}"
        else:
            message_id = f"msg_{uuid4().hex}"
        content_bytes = content.encode("utf-8")
        blob_refs: tuple[BlobReference, ...] = ()
        part_content = content
        part_metadata: dict[str, Any] = {}
        if len(content_bytes) > INLINE_CONTENT_LIMIT_BYTES:
            blob = self.put_blob(content_bytes, content_type="text/plain; charset=utf-8")
            blob_refs = (BlobReference(content_hash=blob.content_hash, role="message.content"),)
            part_content = ""
            part_metadata = {
                "bytes": len(content_bytes),
                "content_hash": blob.content_hash,
                "content_type": blob.content_type,
            }
        payload: dict[str, Any] = {
            "message_id": message_id,
            "role": role,
            "parts": [
                {
                    "kind": "text",
                    "content": part_content,
                    "metadata": part_metadata,
                }
            ],
            "status": "partial" if partial else "complete",
            "replayable": not partial,
        }
        if interruption_reason is not None:
            payload["interruption_reason"] = interruption_reason
        return _PreparedMessage(
            event_type="message.assistant.partial" if partial else f"message.{role}",
            payload=payload,
            blob_refs=blob_refs,
        )

    def reset_context(
        self,
        session_id: str,
        *,
        idempotency_key: str | None = None,
        lease_token: str | None = None,
    ) -> SessionEvent:
        return self.append_event(
            session_id,
            "context.reset",
            {},
            idempotency_key=idempotency_key,
            lease_token=lease_token,
        )

    def hide_message(
        self,
        session_id: str,
        message_id: str,
        *,
        idempotency_key: str | None = None,
        lease_token: str | None = None,
    ) -> SessionEvent:
        view = self.rebuild_session_view(session_id)
        if not any(message.id == message_id for message in view.session_history):
            raise KeyError(f"message not found in session {session_id}: {message_id}")
        return self.append_event(
            session_id,
            "message.hidden",
            {"message_id": message_id},
            idempotency_key=idempotency_key,
            lease_token=lease_token,
        )

    def archive_session(
        self,
        session_id: str,
        *,
        lease_token: str | None = None,
    ) -> SessionRecord:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_writable_session(
                connection,
                session_id,
                lease_token=lease_token,
            )
            self._append_event_in_transaction(
                connection,
                session_id=session_id,
                event_type="session.archived",
                payload={},
                created_at=now,
            )
            connection.execute(
                "update sessions set archived_at = ? where id = ?",
                (now, session_id),
            )
        return self._require_session_record(session_id)

    def unarchive_session(
        self,
        session_id: str,
        *,
        lease_token: str | None = None,
    ) -> SessionRecord:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_leaseable_session(
                connection,
                session_id,
                lease_token=lease_token,
            )
            row = connection.execute(
                """
                select status, archived_at, deleted_at
                from sessions
                where id = ?
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"session not found: {session_id}")
            if row["status"] == "corrupt" or row["deleted_at"] is not None:
                raise SessionReadOnlyError(f"session cannot be unarchived: {session_id}")
            if row["archived_at"] is not None:
                self._append_event_in_transaction(
                    connection,
                    session_id=session_id,
                    event_type="session.unarchived",
                    payload={},
                    created_at=now,
                )
                connection.execute(
                    "update sessions set archived_at = null where id = ?",
                    (session_id,),
                )
        return self._require_session_record(session_id)

    def delete_session(
        self,
        session_id: str,
        *,
        retention_days: int = 30,
        lease_token: str | None = None,
    ) -> SessionRecord:
        now = east_eight_now()
        deleted_at = format_timestamp(now)
        purge_after = format_timestamp(now + timedelta(days=retention_days))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_writable_session(
                connection,
                session_id,
                lease_token=lease_token,
            )
            row = connection.execute(
                """
                select status, deleted_at
                from sessions
                where id = ?
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"session not found: {session_id}")
            if row["status"] == "corrupt":
                raise SessionReadOnlyError(
                    f"corrupt sessions must be permanently purged: {session_id}"
                )
            if row["deleted_at"] is None:
                self._append_event_in_transaction(
                    connection,
                    session_id=session_id,
                    event_type="session.deleted",
                    payload={"purge_after": purge_after},
                    created_at=deleted_at,
                )
                connection.execute(
                    """
                    update sessions
                    set deleted_at = ?, purge_after = ?
                    where id = ?
                    """,
                    (deleted_at, purge_after, session_id),
                )
        return self._require_session_record(session_id)

    def restore_session(
        self,
        session_id: str,
        *,
        lease_token: str | None = None,
    ) -> SessionRecord:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_leaseable_session(
                connection,
                session_id,
                lease_token=lease_token,
            )
            row = connection.execute(
                """
                select status, deleted_at
                from sessions
                where id = ?
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"session not found: {session_id}")
            if row["status"] == "corrupt":
                raise SessionReadOnlyError(f"session is corrupt and read-only: {session_id}")
            if row["deleted_at"] is not None:
                self._append_event_in_transaction(
                    connection,
                    session_id=session_id,
                    event_type="session.restored",
                    payload={},
                    created_at=now,
                )
                connection.execute(
                    """
                    update sessions
                    set deleted_at = null, purge_after = null
                    where id = ?
                    """,
                    (session_id,),
                )
        return self._require_session_record(session_id)

    def purge_session(self, session_id: str) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "delete from sessions where id = ?",
                (session_id,),
            )
            return cursor.rowcount == 1

    def purge_expired_sessions(
        self,
        *,
        as_of: datetime | None = None,
    ) -> tuple[str, ...]:
        cutoff = format_timestamp(as_of or east_eight_now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                select id
                from sessions
                where purge_after is not null and purge_after <= ?
                order by purge_after, id
                """,
                (cutoff,),
            ).fetchall()
            session_ids = tuple(str(row["id"]) for row in rows)
            connection.executemany(
                "delete from sessions where id = ?",
                ((session_id,) for session_id in session_ids),
            )
            return session_ids

    def collect_orphan_blobs(self) -> int:
        return self._blob_store.collect_orphans()

    def fork_session(
        self,
        source_session_id: str,
        *,
        through_sequence: int | None = None,
        workspace_root: str | Path | None = None,
        title: str | None = None,
        lease_token: str | None = None,
    ) -> SessionRecord:
        self.verify_session(source_session_id)
        with self._connect() as connection:
            self._require_writable_session(
                connection,
                source_session_id,
                lease_token=lease_token,
            )
        source = self._require_session_record(source_session_id)
        if source.deleted_at is not None:
            raise SessionReadOnlyError(f"deleted session cannot be forked: {source_session_id}")
        source_events = self.list_events(source_session_id)
        latest_sequence = source_events[-1].sequence
        if through_sequence is None:
            latest_turn_start = max(
                (event.sequence for event in source_events if event.type == "turn.started"),
                default=0,
            )
            latest_turn_terminal = max(
                (
                    event.sequence
                    for event in source_events
                    if event.type in {"turn.completed", "turn.interrupted"}
                ),
                default=0,
            )
            if latest_turn_start > latest_turn_terminal:
                raise ValueError("active turn must reach a turn boundary before fork")
            boundary = next(
                (
                    event
                    for event in reversed(source_events)
                    if event.type in FORK_BOUNDARY_EVENT_TYPES
                ),
                None,
            )
            if boundary is None:
                raise ValueError("session has no completed turn boundary")
            selected_sequence = boundary.sequence
        else:
            selected_sequence = through_sequence
        if selected_sequence < 1 or selected_sequence > latest_sequence:
            raise ValueError(f"invalid fork sequence: {selected_sequence}")
        boundary = source_events[selected_sequence - 1]
        if boundary.type not in FORK_BOUNDARY_EVENT_TYPES:
            raise ValueError(f"fork sequence is not a completed turn boundary: {selected_sequence}")

        copied_events = [
            event
            for event in source_events
            if event.sequence <= selected_sequence and _is_forkable_event(event)
        ]
        source_view = rebuild_session_view(
            source_session_id,
            source_events[:selected_sequence],
            blob_loader=self._load_blob_bytes,
        )
        source_metadata = _event_metadata(source_view.metadata)
        child_id = f"sess_{uuid4().hex}"
        child_workspace = str(Path(workspace_root or source.workspace_root).expanduser().resolve())
        source_title = str(source_view.metadata.get("title") or source.title)
        child_title = title or f"{source_title} (fork)"
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                insert into sessions(
                    id, workspace_root, title, status, created_at, updated_at,
                    next_sequence, version, metadata_json
                ) values (?, ?, ?, 'idle', ?, ?, 1, 1, ?)
                """,
                (
                    child_id,
                    child_workspace,
                    child_title,
                    now,
                    now,
                    _json_dumps(source_metadata),
                ),
            )
            self._append_event_in_transaction(
                connection,
                session_id=child_id,
                event_type="session.created",
                payload={
                    "session_id": child_id,
                    "title": child_title,
                    "workspace_root": child_workspace,
                    **source_metadata,
                },
                created_at=now,
            )
            self._append_event_in_transaction(
                connection,
                session_id=child_id,
                event_type="session.forked",
                payload={
                    "source_session_id": source_session_id,
                    "source_through_sequence": selected_sequence,
                },
                created_at=now,
                source_session_id=source_session_id,
            )
            for event in copied_events:
                copied_payload = event.payload
                if event.type == "session.metadata_updated" and "title" in copied_payload:
                    copied_payload = {
                        key: value for key, value in copied_payload.items() if key != "title"
                    }
                    if not copied_payload:
                        continue
                self._append_event_in_transaction(
                    connection,
                    session_id=child_id,
                    event_type=event.type,
                    payload=copied_payload,
                    created_at=event.created_at,
                    turn_id=event.turn_id,
                    source_session_id=source_session_id,
                    source_event_id=event.id,
                    blob_refs=event.blob_refs,
                )
            connection.execute(
                "update sessions set updated_at = ? where id = ?",
                (now, child_id),
            )
        return self._require_session_record(child_id)

    def rebuild_session_view(self, session_id: str) -> SessionView:
        if self.get_session(session_id) is None:
            raise KeyError(f"session not found: {session_id}")
        self.verify_session(session_id)
        try:
            return rebuild_session_view(
                session_id,
                self.list_events(session_id),
                blob_loader=self._load_blob_bytes,
            )
        except (KeyError, TypeError, UnicodeError, ValueError) as error:
            self._mark_session_corrupt(session_id)
            raise SessionCorruptError(
                f"session {session_id} contains an invalid event payload: {error}"
            ) from error

    def put_blob(self, data: bytes, *, content_type: str) -> StoredBlob:
        return self._blob_store.put(data, content_type=content_type)

    def get_blob(self, content_hash: str) -> StoredBlob | None:
        return self._blob_store.get(content_hash)

    def _load_blob_bytes(self, content_hash: str) -> bytes:
        return self._blob_store.load_bytes(content_hash)

    def list_event_blob_refs(self, event_id: str) -> tuple[BlobReference, ...]:
        return self._blob_store.list_event_refs(event_id)

    def verify_session(self, session_id: str) -> None:
        issue = self._integrity_verifier.find_issue(session_id)
        if issue is not None:
            self._mark_session_corrupt(session_id)
            raise SessionCorruptError(f"session {session_id} is corrupt: {issue}")

    def _mark_session_corrupt(self, session_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                update sessions
                set status = 'corrupt', updated_at = ?
                where id = ?
                """,
                (_now(), session_id),
            )

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                select id, workspace_root, title, status, created_at, updated_at,
                       archived_at, deleted_at, purge_after, metadata_json,
                       message_count, user_turn_count, latest_user_preview,
                       latest_assistant_preview, provider, model,
                       last_checkpoint_id, last_compacted_at
                from sessions
                where id = ?
                """,
                (session_id,),
            ).fetchone()
        return _session_from_row(row) if row is not None else None

    def _require_session_record(self, session_id: str) -> SessionRecord:
        record = self.get_session(session_id)
        if record is None:
            raise KeyError(f"session not found: {session_id}")
        return record

    def list_sessions(
        self,
        *,
        include_archived: bool = False,
        include_deleted: bool = False,
    ) -> list[SessionRecord]:
        clauses = []
        if not include_archived:
            clauses.append("archived_at is null")
        if not include_deleted:
            clauses.append("deleted_at is null")
        where = f"where {' and '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                select id, workspace_root, title, status, created_at, updated_at,
                       archived_at, deleted_at, purge_after, metadata_json,
                       message_count, user_turn_count, latest_user_preview,
                       latest_assistant_preview, provider, model,
                       last_checkpoint_id, last_compacted_at
                from sessions
                {where}
                order by updated_at desc,
                         (
                             select max(event.rowid)
                             from session_events as event
                             where event.session_id = sessions.id
                         ) desc,
                         id desc
                """
            ).fetchall()
        return [_session_from_row(row) for row in rows]

    def list_events(self, session_id: str) -> list[SessionEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select id, session_id, sequence, type, payload_json, schema_version,
                       created_at, previous_event_hash, event_hash, turn_id,
                       idempotency_key, source_session_id, source_event_id,
                       blob_refs_json
                from session_events
                where session_id = ?
                order by sequence
                """,
                (session_id,),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def _append_event_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        created_at: str | None = None,
        turn_id: str | None = None,
        idempotency_key: str | None = None,
        source_session_id: str | None = None,
        source_event_id: str | None = None,
        blob_refs: tuple[BlobReference, ...] = (),
    ) -> SessionEvent:
        validate_event_payload(event_type, payload)
        session = connection.execute(
            "select next_sequence from sessions where id = ?",
            (session_id,),
        ).fetchone()
        if session is None:
            raise KeyError(f"session not found: {session_id}")
        sequence = int(session["next_sequence"])
        previous = connection.execute(
            """
            select event_hash from session_events
            where session_id = ?
            order by sequence desc
            limit 1
            """,
            (session_id,),
        ).fetchone()
        previous_hash = str(previous["event_hash"]) if previous is not None else None
        event_id = f"evt_{uuid4().hex}"
        timestamp = created_at or _now()
        payload_json = _json_dumps(payload)
        serialized_blob_refs = blob_refs_json(blob_refs)
        event_hash = EventHashMaterial(
            event_id=event_id,
            session_id=session_id,
            sequence=sequence,
            event_type=event_type,
            payload_json=payload_json,
            schema_version=EVENT_SCHEMA_VERSION,
            created_at=timestamp,
            previous_event_hash=previous_hash,
            turn_id=turn_id,
            idempotency_key=idempotency_key,
            source_session_id=source_session_id,
            source_event_id=source_event_id,
            blob_refs_json=serialized_blob_refs,
        ).digest()
        connection.execute(
            """
            insert into session_events(
                id, session_id, sequence, turn_id, type, schema_version,
                payload_json, idempotency_key, previous_event_hash, event_hash,
                created_at, source_session_id, source_event_id, blob_refs_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                session_id,
                sequence,
                turn_id,
                event_type,
                EVENT_SCHEMA_VERSION,
                payload_json,
                idempotency_key,
                previous_hash,
                event_hash,
                timestamp,
                source_session_id,
                source_event_id,
                serialized_blob_refs,
            ),
        )
        for ordinal, reference in enumerate(blob_refs):
            blob = connection.execute(
                "select 1 from blobs where content_hash = ?",
                (reference.content_hash,),
            ).fetchone()
            if blob is None:
                raise KeyError(f"blob not found: {reference.content_hash}")
            connection.execute(
                """
                insert into event_blob_refs(event_id, ordinal, content_hash, role)
                values (?, ?, ?, ?)
                """,
                (event_id, ordinal, reference.content_hash, reference.role),
            )
        self._update_catalog_projection(
            connection,
            session_id=session_id,
            event_type=event_type,
            payload=payload,
            timestamp=timestamp,
            blob_refs=blob_refs,
        )
        connection.execute(
            """
            update sessions
            set next_sequence = ?, version = version + 1, updated_at = ?
            where id = ?
            """,
            (sequence + 1, timestamp, session_id),
        )
        return SessionEvent(
            id=event_id,
            session_id=session_id,
            sequence=sequence,
            type=event_type,
            payload=dict(payload),
            schema_version=EVENT_SCHEMA_VERSION,
            created_at=timestamp,
            previous_event_hash=previous_hash,
            event_hash=event_hash,
            turn_id=turn_id,
            idempotency_key=idempotency_key,
            source_session_id=source_session_id,
            source_event_id=source_event_id,
            blob_refs=blob_refs,
        )

    def _prepare_context_checkpoint(
        self,
        checkpoint: dict[str, Any],
    ) -> tuple[dict[str, Any], tuple[BlobReference, ...]]:
        messages = list(checkpoint["messages"])
        serialized = _json_dumps(messages).encode("utf-8")
        if len(serialized) <= INLINE_CONTENT_LIMIT_BYTES:
            return {
                "checkpoint_id": str(checkpoint["checkpoint_id"]),
                "messages": messages,
            }, ()
        blob = self.put_blob(
            serialized,
            content_type="application/json; charset=utf-8",
        )
        return {
            "checkpoint_id": str(checkpoint["checkpoint_id"]),
            "messages": [],
            "messages_content_hash": blob.content_hash,
            "messages_count": len(messages),
        }, (
            BlobReference(
                content_hash=blob.content_hash,
                role="context.checkpoint.messages",
            ),
        )

    def _annotate_context_checkpoint_messages(
        self,
        session_id: str,
        checkpoint: dict[str, Any],
        *,
        pending_assistant: dict[str, Any],
    ) -> dict[str, Any]:
        messages = [
            dict(message) for message in checkpoint["messages"] if isinstance(message, dict)
        ]
        candidates = [
            {
                "message_id": message.id,
                "role": message.role,
                "content": message.content,
            }
            for message in self.rebuild_session_view(session_id).model_messages
        ]
        candidates.append(
            {
                "message_id": str(pending_assistant["message_id"]),
                "role": str(pending_assistant["role"]),
                "content": _message_payload_content(
                    pending_assistant,
                    blob_loader=self._load_blob_bytes,
                ),
            }
        )
        upper_bound = len(candidates)
        for message in reversed(messages):
            matching_index = next(
                (
                    index
                    for index in range(upper_bound - 1, -1, -1)
                    if message.get("role") == candidates[index]["role"]
                    and message.get("content") == candidates[index]["content"]
                ),
                None,
            )
            if matching_index is None:
                continue
            message["source_message_id"] = candidates[matching_index]["message_id"]
            upper_bound = matching_index
        return {**checkpoint, "messages": messages}

    def _update_catalog_projection(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        timestamp: str,
        blob_refs: tuple[BlobReference, ...],
    ) -> None:
        if event_type.startswith("message."):
            role = str(payload.get("role") or "")
            preview = _message_preview(payload)
            if preview is None:
                content_reference = next(
                    (reference for reference in blob_refs if reference.role == "message.content"),
                    None,
                )
                if content_reference is not None:
                    preview = _text_preview(
                        self._load_blob_bytes(content_reference.content_hash).decode("utf-8")
                    )
            if role == "user":
                connection.execute(
                    """
                    update sessions
                    set message_count = message_count + 1,
                        user_turn_count = user_turn_count + 1,
                        latest_user_preview = ?
                    where id = ?
                    """,
                    (preview, session_id),
                )
            elif role == "assistant":
                connection.execute(
                    """
                    update sessions
                    set message_count = message_count + 1,
                        latest_assistant_preview = ?
                    where id = ?
                    """,
                    (preview, session_id),
                )
            elif role == "tool":
                connection.execute(
                    """
                    update sessions
                    set message_count = message_count + 1
                    where id = ?
                    """,
                    (session_id,),
                )
        elif event_type == "context.compacted":
            connection.execute(
                """
                update sessions
                set provider = ?, model = ?, last_compacted_at = ?
                where id = ?
                """,
                (
                    str(payload.get("provider") or "") or None,
                    str(payload.get("model") or "") or None,
                    timestamp,
                    session_id,
                ),
            )
        elif event_type == "context.checkpoint_created":
            connection.execute(
                """
                update sessions
                set last_checkpoint_id = ?
                where id = ?
                """,
                (str(payload["checkpoint_id"]), session_id),
            )
        elif event_type == "context.reset":
            connection.execute(
                """
                update sessions
                set provider = null, model = null,
                    last_checkpoint_id = null, last_compacted_at = null
                where id = ?
                """,
                (session_id,),
            )

    def _event_idempotency_key_exists(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        idempotency_key: str,
    ) -> bool:
        row = connection.execute(
            """
            select 1
            from session_events
            where session_id = ? and idempotency_key = ?
            """,
            (session_id, idempotency_key),
        ).fetchone()
        return row is not None

    def _create_session_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        workspace_root: str,
        title: str | None,
        metadata: dict[str, Any] | None,
        created_at: str | None = None,
    ) -> str:
        session_id = f"sess_{uuid4().hex}"
        resolved_title = title or session_id
        now = created_at or _now()
        session_metadata = dict(metadata or {})
        connection.execute(
            """
            insert into sessions(
                id, workspace_root, title, status, created_at, updated_at,
                next_sequence, version, metadata_json
            ) values (?, ?, ?, 'idle', ?, ?, 1, 1, ?)
            """,
            (
                session_id,
                workspace_root,
                resolved_title,
                now,
                now,
                _json_dumps(session_metadata),
            ),
        )
        payload = {
            "session_id": session_id,
            "title": resolved_title,
            "workspace_root": workspace_root,
        }
        payload.update(_event_metadata(session_metadata))
        self._append_event_in_transaction(
            connection,
            session_id=session_id,
            event_type="session.created",
            payload=payload,
            created_at=now,
        )
        return session_id

    def _create_child_session_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        parent_session_id: str,
        relation_type: str,
        title: str | None,
        metadata: dict[str, Any] | None,
        relationship_metadata: dict[str, Any],
        created_at: str,
        lease_token: str | None,
    ) -> str:
        self._require_writable_session(
            connection,
            parent_session_id,
            lease_token=lease_token,
        )
        parent = connection.execute(
            "select workspace_root from sessions where id = ?",
            (parent_session_id,),
        ).fetchone()
        child_session_id = self._create_session_in_transaction(
            connection,
            workspace_root=str(parent["workspace_root"]),
            title=title,
            metadata=metadata,
            created_at=created_at,
        )
        relation_payload = {
            "parent_session_id": parent_session_id,
            "child_session_id": child_session_id,
            "relation_type": relation_type,
            "metadata": relationship_metadata,
        }
        self._append_event_in_transaction(
            connection,
            session_id=parent_session_id,
            event_type="session.child_linked",
            payload=relation_payload,
            created_at=created_at,
        )
        self._append_event_in_transaction(
            connection,
            session_id=child_session_id,
            event_type="session.parent_linked",
            payload=relation_payload,
            created_at=created_at,
        )
        connection.execute(
            """
            insert into session_relationships(
                parent_session_id, child_session_id, relation_type,
                created_at, metadata_json
            ) values (?, ?, ?, ?, ?)
            """,
            (
                parent_session_id,
                child_session_id,
                relation_type,
                created_at,
                _json_dumps(relationship_metadata),
            ),
        )
        return child_session_id

    def _require_writable_session(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        *,
        lease_token: str | None = None,
        enforce_lease: bool = True,
    ) -> None:
        row = connection.execute(
            """
            select status, archived_at, deleted_at
            from sessions
            where id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"session not found: {session_id}")
        if row["status"] == "corrupt":
            raise SessionReadOnlyError(f"session is corrupt and read-only: {session_id}")
        if row["archived_at"] is not None:
            raise SessionReadOnlyError(f"session is archived: {session_id}")
        if row["deleted_at"] is not None:
            raise SessionReadOnlyError(f"session is deleted: {session_id}")
        if not enforce_lease:
            return
        SessionLeaseStore.require_active_token(
            connection,
            session_id,
            lease_token,
            now=_now(),
        )

    def _require_leaseable_session(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        *,
        lease_token: str | None = None,
        enforce_lease: bool = True,
    ) -> None:
        row = connection.execute(
            "select status from sessions where id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"session not found: {session_id}")
        if row["status"] == "corrupt":
            raise SessionReadOnlyError(f"session is corrupt and read-only: {session_id}")
        if not enforce_lease:
            return
        SessionLeaseStore.require_active_token(
            connection,
            session_id,
            lease_token,
            now=_now(),
        )

    def _ensure_schema(self) -> None:
        ensure_schema(self.db_path)

    def _connect(self, *, timeout_seconds: float = 5.0) -> sqlite3.Connection:
        return connect(self.db_path, timeout_seconds=timeout_seconds)


def _session_from_row(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(
        id=str(row["id"]),
        workspace_root=str(row["workspace_root"]),
        title=str(row["title"]),
        status=str(row["status"]),
        created_at=normalize_timestamp(str(row["created_at"])),
        updated_at=normalize_timestamp(str(row["updated_at"])),
        archived_at=normalize_optional_timestamp(row["archived_at"]),
        deleted_at=normalize_optional_timestamp(row["deleted_at"]),
        purge_after=normalize_optional_timestamp(row["purge_after"]),
        metadata=json.loads(row["metadata_json"]),
        message_count=int(row["message_count"]),
        user_turn_count=int(row["user_turn_count"]),
        latest_user_preview=row["latest_user_preview"],
        latest_assistant_preview=row["latest_assistant_preview"],
        provider=row["provider"],
        model=row["model"],
        last_checkpoint_id=row["last_checkpoint_id"],
        last_compacted_at=normalize_optional_timestamp(row["last_compacted_at"]),
    )


def _message_preview(payload: dict[str, Any], *, limit: int = 160) -> str | None:
    parts = payload.get("parts")
    if not isinstance(parts, list):
        return None
    text = " ".join(
        str(part.get("content") or "")
        for part in parts
        if isinstance(part, dict) and part.get("kind", "text") in {"text", "tool_result"}
    )
    return _text_preview(text, limit=limit)


def _text_preview(text: str, *, limit: int = 160) -> str | None:
    normalized = " ".join(text.split())
    if not normalized:
        return None
    return normalized if len(normalized) <= limit else f"{normalized[: limit - 1]}…"


def _message_payload_content(
    payload: dict[str, Any],
    *,
    blob_loader=None,
) -> str:
    parts = payload.get("parts")
    if not isinstance(parts, list):
        return ""
    content: list[str] = []
    for part in parts:
        if not isinstance(part, dict) or part.get("kind", "text") not in {
            "text",
            "tool_result",
        }:
            continue
        text = str(part.get("content") or "")
        metadata = part.get("metadata")
        if (
            not text
            and blob_loader is not None
            and isinstance(metadata, dict)
            and metadata.get("content_hash")
        ):
            text = blob_loader(str(metadata["content_hash"])).decode("utf-8")
        content.append(text)
    return "".join(content)


def _event_from_row(row: sqlite3.Row) -> SessionEvent:
    schema_version = int(row["schema_version"])
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        raise TypeError("event payload must be a JSON object")
    payload = upcast_event_payload(str(row["type"]), payload, schema_version)
    validate_event_payload(str(row["type"]), payload)
    return SessionEvent(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        sequence=int(row["sequence"]),
        type=str(row["type"]),
        payload=payload,
        schema_version=schema_version,
        created_at=normalize_timestamp(str(row["created_at"])),
        previous_event_hash=row["previous_event_hash"],
        event_hash=str(row["event_hash"]),
        turn_id=row["turn_id"],
        idempotency_key=row["idempotency_key"],
        source_session_id=row["source_session_id"],
        source_event_id=row["source_event_id"],
        blob_refs=blob_refs_from_json(str(row["blob_refs_json"])),
    )


def _relationship_from_row(row: sqlite3.Row) -> SessionRelationship:
    return SessionRelationship(
        parent_session_id=str(row["parent_session_id"]),
        child_session_id=str(row["child_session_id"]),
        relation_type=str(row["relation_type"]),
        created_at=normalize_timestamp(str(row["created_at"])),
        metadata=json.loads(row["metadata_json"]),
    )


def _json_dumps(value: Any) -> str:
    return canonical_json(value)


def _is_forkable_event(event: SessionEvent) -> bool:
    if event.type in {
        "session.created",
        "session.archived",
        "session.unarchived",
        "session.deleted",
        "session.restored",
        "session.forked",
    }:
        return False
    return not event.type.startswith(("approval.", "lease.", "pending_action.", "turn."))


def _event_metadata(
    metadata: dict[str, Any],
    *,
    preserve_none: bool = False,
) -> dict[str, Any]:
    reserved = {"session_id", "title", "workspace_root"}
    return {
        key: value
        for key, value in metadata.items()
        if key not in reserved and (preserve_none or value is not None)
    }


def _now() -> str:
    return now_timestamp()
