from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

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
    SessionEvent,
    SessionMessage,
    SessionRecord,
    SessionView,
    StoredBlob,
)
from paicli.session.replay import message_from_event, rebuild_session_view
from paicli.session.schema import connect, ensure_schema
from paicli.session.validation import validate_event_payload
from paicli.session.verification import SessionIntegrityVerifier
from paicli.session.versions import EVENT_SCHEMA_VERSION, upcast_event_payload

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
        session_id = f"sess_{uuid4().hex}"
        canonical_workspace = str(Path(workspace_root).expanduser().resolve())
        resolved_title = title or session_id
        now = _now()
        session_metadata = dict(metadata or {})
        payload = {
            "session_id": session_id,
            "title": resolved_title,
            "workspace_root": canonical_workspace,
        }
        payload.update(_event_metadata(session_metadata))
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
                    session_id,
                    canonical_workspace,
                    resolved_title,
                    now,
                    now,
                    _json_dumps(session_metadata),
                ),
            )
            self._append_event_in_transaction(
                connection,
                session_id=session_id,
                event_type="session.created",
                payload=payload,
                created_at=now,
            )
        record = self.get_session(session_id)
        if record is None:  # pragma: no cover - the transaction above guarantees the row
            raise RuntimeError(f"created session disappeared: {session_id}")
        return record

    def update_session_metadata(
        self,
        session_id: str,
        *,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
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
            self._require_writable_session(connection, session_id)
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
    ) -> SessionEvent:
        if event_type in RESERVED_PUBLIC_EVENT_TYPES:
            raise ValueError(
                f"reserved event type must use its transactional repository method: {event_type}"
            )
        payload_json = _json_dumps(payload)
        normalized_blob_refs = tuple(blob_refs)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_writable_session(connection, session_id)
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
        )
        return message_from_event(event, blob_loader=self._load_blob_bytes)

    def begin_turn(self, session_id: str, *, turn_id: str, user_content: str) -> SessionMessage:
        prepared = self._prepare_message(
            session_id,
            role="user",
            content=user_content,
            idempotency_key=f"{turn_id}:user",
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_writable_session(connection, session_id)
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
    ) -> SessionMessage:
        prepared = self._prepare_message(
            session_id,
            role="assistant",
            content=assistant_content,
            idempotency_key=f"{turn_id}:assistant",
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_writable_session(connection, session_id)
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
        return message_from_event(event, blob_loader=self._load_blob_bytes)

    def interrupt_turn(
        self,
        session_id: str,
        *,
        turn_id: str,
        assistant_content: str,
        reason: str,
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
            self._require_writable_session(connection, session_id)
            if prepared is not None:
                message_event = self._append_event_in_transaction(
                    connection,
                    session_id=session_id,
                    event_type=prepared.event_type,
                    payload=prepared.payload,
                    turn_id=turn_id,
                    idempotency_key=f"{turn_id}:assistant-partial",
                    blob_refs=prepared.blob_refs,
                )
            self._append_event_in_transaction(
                connection,
                session_id=session_id,
                event_type="turn.interrupted",
                payload={"reason": reason},
                turn_id=turn_id,
                idempotency_key=f"{turn_id}:interrupted",
            )
        if message_event is None:
            return None
        return message_from_event(message_event, blob_loader=self._load_blob_bytes)

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
    ) -> SessionEvent:
        return self.append_event(
            session_id,
            "context.reset",
            {},
            idempotency_key=idempotency_key,
        )

    def hide_message(
        self,
        session_id: str,
        message_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> SessionEvent:
        view = self.rebuild_session_view(session_id)
        if not any(message.id == message_id for message in view.session_history):
            raise KeyError(f"message not found in session {session_id}: {message_id}")
        return self.append_event(
            session_id,
            "message.hidden",
            {"message_id": message_id},
            idempotency_key=idempotency_key,
        )

    def archive_session(self, session_id: str) -> SessionRecord:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_writable_session(connection, session_id)
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

    def unarchive_session(self, session_id: str) -> SessionRecord:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
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

    def delete_session(self, session_id: str, *, retention_days: int = 30) -> SessionRecord:
        now = datetime.now(UTC)
        deleted_at = now.isoformat()
        purge_after = (now + timedelta(days=retention_days)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
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

    def restore_session(self, session_id: str) -> SessionRecord:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
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
        cutoff = (as_of or datetime.now(UTC)).isoformat()
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
    ) -> SessionRecord:
        self.verify_session(source_session_id)
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
                       archived_at, deleted_at, purge_after, metadata_json
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
                       archived_at, deleted_at, purge_after, metadata_json
                from sessions
                {where}
                order by updated_at desc, id desc
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

    def _require_writable_session(
        self,
        connection: sqlite3.Connection,
        session_id: str,
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

    def _ensure_schema(self) -> None:
        ensure_schema(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        return connect(self.db_path)


def _session_from_row(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(
        id=str(row["id"]),
        workspace_root=str(row["workspace_root"]),
        title=str(row["title"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        archived_at=row["archived_at"],
        deleted_at=row["deleted_at"],
        purge_after=row["purge_after"],
        metadata=json.loads(row["metadata_json"]),
    )


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
        created_at=str(row["created_at"]),
        previous_event_hash=row["previous_event_hash"],
        event_hash=str(row["event_hash"]),
        turn_id=row["turn_id"],
        idempotency_key=row["idempotency_key"],
        source_session_id=row["source_session_id"],
        source_event_id=row["source_event_id"],
        blob_refs=blob_refs_from_json(str(row["blob_refs_json"])),
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
    return datetime.now(UTC).isoformat()
