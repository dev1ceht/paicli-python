from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from paicli.session.errors import (
    SessionCorruptError,
    SessionIdempotencyConflictError,
    SessionReadOnlyError,
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

DATABASE_SCHEMA_VERSION = 1
EVENT_SCHEMA_VERSION = 1


class SessionRepository:
    """SQLite-backed authoritative store for durable sessions."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

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
        payload_json = _json_dumps(payload)
        normalized_blob_refs = _normalize_blob_refs(blob_refs)
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
        if role not in {"user", "assistant", "tool"}:
            raise ValueError(f"unsupported message role: {role}")
        if partial and role != "assistant":
            raise ValueError("only assistant messages can be partial")
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
        event = self.append_event(
            session_id,
            "message.assistant.partial" if partial else f"message.{role}",
            payload,
            turn_id=turn_id,
            idempotency_key=idempotency_key,
        )
        return message_from_event(event)

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
        if not any(message.id == message_id for message in view.transcript):
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
            if row["archived_at"] is None:
                return self._require_session_record(session_id)
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
            if row["deleted_at"] is not None:
                return self._require_session_record(session_id)
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
            if row["deleted_at"] is None:
                return self._require_session_record(session_id)
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
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                delete from blobs
                where not exists (
                    select 1
                    from event_blob_refs
                    where event_blob_refs.content_hash = blobs.content_hash
                )
                """
            )
            return cursor.rowcount

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
        selected_sequence = through_sequence or latest_sequence
        if selected_sequence < 1 or selected_sequence > latest_sequence:
            raise ValueError(f"invalid fork sequence: {selected_sequence}")
        if selected_sequence != latest_sequence:
            boundary = source_events[selected_sequence - 1]
            if boundary.type not in {"turn.completed", "turn.interrupted", "context.reset"}:
                raise ValueError(
                    f"fork sequence is not a completed turn boundary: {selected_sequence}"
                )

        copied_events = [
            event
            for event in source_events
            if event.sequence <= selected_sequence and _is_forkable_event(event)
        ]
        child_id = f"sess_{uuid4().hex}"
        child_workspace = str(Path(workspace_root or source.workspace_root).expanduser().resolve())
        child_title = title or f"{source.title} (fork)"
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
                    _json_dumps(source.metadata),
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
                    **_event_metadata(source.metadata),
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
                self._append_event_in_transaction(
                    connection,
                    session_id=child_id,
                    event_type=event.type,
                    payload=event.payload,
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
        return rebuild_session_view(session_id, self.list_events(session_id))

    def put_blob(self, data: bytes, *, content_type: str) -> StoredBlob:
        content_hash = hashlib.sha256(data).hexdigest()
        existing = self.get_blob(content_hash)
        if existing is not None:
            return existing
        compressed = zlib.compress(data)
        if len(compressed) < len(data):
            compression = "zlib"
            stored_data = compressed
        else:
            compression = "none"
            stored_data = data
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                insert or ignore into blobs(
                    content_hash, content_type, compression, original_size,
                    stored_size, data, created_at
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    content_hash,
                    content_type,
                    compression,
                    len(data),
                    len(stored_data),
                    stored_data,
                    now,
                ),
            )
        stored = self.get_blob(content_hash)
        if stored is None:  # pragma: no cover - an insert or racing insert must win
            raise RuntimeError(f"blob disappeared after insert: {content_hash}")
        return stored

    def get_blob(self, content_hash: str) -> StoredBlob | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                select content_hash, content_type, compression, original_size,
                       stored_size, data, created_at
                from blobs
                where content_hash = ?
                """,
                (content_hash,),
            ).fetchone()
        if row is None:
            return None
        raw = bytes(row["data"])
        compression = str(row["compression"])
        data = zlib.decompress(raw) if compression == "zlib" else raw
        return StoredBlob(
            content_hash=str(row["content_hash"]),
            content_type=str(row["content_type"]),
            compression=compression,
            original_size=int(row["original_size"]),
            stored_size=int(row["stored_size"]),
            data=data,
            created_at=str(row["created_at"]),
        )

    def list_event_blob_refs(self, event_id: str) -> tuple[BlobReference, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select content_hash, role
                from event_blob_refs
                where event_id = ?
                order by role, content_hash
                """,
                (event_id,),
            ).fetchall()
        return tuple(
            BlobReference(content_hash=str(row["content_hash"]), role=str(row["role"]))
            for row in rows
        )

    def verify_session(self, session_id: str) -> None:
        with self._connect() as connection:
            session = connection.execute(
                "select id from sessions where id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise KeyError(f"session not found: {session_id}")
            rows = connection.execute(
                """
                select id, session_id, sequence, turn_id, type, schema_version,
                       payload_json, idempotency_key, previous_event_hash, event_hash,
                       created_at, source_session_id, source_event_id, blob_refs_json
                from session_events
                where session_id = ?
                order by sequence
                """,
                (session_id,),
            ).fetchall()
            reference_rows = connection.execute(
                """
                select refs.event_id, refs.content_hash, refs.role
                from event_blob_refs as refs
                join session_events as events on events.id = refs.event_id
                where events.session_id = ?
                order by refs.event_id, refs.role, refs.content_hash
                """,
                (session_id,),
            ).fetchall()
            blob_rows = connection.execute(
                """
                select distinct blobs.content_hash, blobs.compression, blobs.original_size,
                                blobs.stored_size, blobs.data
                from blobs
                join event_blob_refs as refs
                  on refs.content_hash = blobs.content_hash
                join session_events as events
                  on events.id = refs.event_id
                where events.session_id = ?
                """,
                (session_id,),
            ).fetchall()

        refs_by_event: dict[str, list[BlobReference]] = {}
        for row in reference_rows:
            refs_by_event.setdefault(str(row["event_id"]), []).append(
                BlobReference(
                    content_hash=str(row["content_hash"]),
                    role=str(row["role"]),
                )
            )
        blobs_by_hash = {str(row["content_hash"]): row for row in blob_rows}
        previous_hash: str | None = None
        issue: str | None = None
        for expected_sequence, row in enumerate(rows, start=1):
            if int(row["sequence"]) != expected_sequence:
                issue = f"event sequence gap: expected {expected_sequence}, got {row['sequence']}"
                break
            schema_version = int(row["schema_version"])
            if not 1 <= schema_version <= EVENT_SCHEMA_VERSION:
                issue = f"unsupported event schema version: {schema_version}"
                break
            if row["previous_event_hash"] != previous_hash:
                issue = f"previous event hash mismatch at sequence {expected_sequence}"
                break
            expected_refs = _blob_refs_from_json(str(row["blob_refs_json"]))
            actual_refs = tuple(refs_by_event.get(str(row["id"]), []))
            if actual_refs != expected_refs:
                issue = f"blob reference mismatch at sequence {expected_sequence}"
                break
            invalid_blob = next(
                (
                    reference.content_hash
                    for reference in expected_refs
                    if not _stored_blob_is_valid(
                        blobs_by_hash.get(reference.content_hash),
                        reference.content_hash,
                    )
                ),
                None,
            )
            if invalid_blob is not None:
                issue = f"missing or corrupt blob {invalid_blob} at sequence {expected_sequence}"
                break
            expected_hash = _event_hash(
                event_id=str(row["id"]),
                session_id=str(row["session_id"]),
                sequence=int(row["sequence"]),
                event_type=str(row["type"]),
                payload_json=str(row["payload_json"]),
                schema_version=schema_version,
                created_at=str(row["created_at"]),
                previous_event_hash=previous_hash,
                turn_id=row["turn_id"],
                idempotency_key=row["idempotency_key"],
                source_session_id=row["source_session_id"],
                source_event_id=row["source_event_id"],
                blob_refs_json=str(row["blob_refs_json"]),
            )
            if str(row["event_hash"]) != expected_hash:
                issue = f"event hash mismatch at sequence {expected_sequence}"
                break
            previous_hash = expected_hash

        if issue is not None:
            with self._connect() as connection:
                connection.execute(
                    """
                    update sessions
                    set status = 'corrupt', updated_at = ?
                    where id = ?
                    """,
                    (_now(), session_id),
                )
            raise SessionCorruptError(f"session {session_id} is corrupt: {issue}")

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
        blob_refs_json = _blob_refs_json(blob_refs)
        event_hash = _event_hash(
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
            blob_refs_json=blob_refs_json,
        )
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
                blob_refs_json,
            ),
        )
        for reference in blob_refs:
            blob = connection.execute(
                "select 1 from blobs where content_hash = ?",
                (reference.content_hash,),
            ).fetchone()
            if blob is None:
                raise KeyError(f"blob not found: {reference.content_hash}")
            connection.execute(
                """
                insert into event_blob_refs(event_id, content_hash, role)
                values (?, ?, ?)
                """,
                (event_id, reference.content_hash, reference.role),
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
        with self._connect() as connection:
            connection.execute(
                """
                create table if not exists schema_migrations (
                    version integer primary key,
                    applied_at text not null
                )
                """
            )
            current = int(connection.execute("pragma user_version").fetchone()[0])
            if current > DATABASE_SCHEMA_VERSION:
                raise RuntimeError(
                    f"session database schema {current} is newer than supported "
                    f"{DATABASE_SCHEMA_VERSION}"
                )
            if current == 0:
                connection.executescript(
                    """
                    create table sessions (
                        id text primary key,
                        workspace_root text not null,
                        title text not null,
                        status text not null,
                        created_at text not null,
                        updated_at text not null,
                        archived_at text,
                        deleted_at text,
                        purge_after text,
                        next_sequence integer not null,
                        version integer not null,
                        metadata_json text not null
                    );

                    create table session_events (
                        id text primary key,
                        session_id text not null references sessions(id) on delete cascade,
                        sequence integer not null,
                        turn_id text,
                        type text not null,
                        schema_version integer not null,
                        payload_json text not null,
                        idempotency_key text,
                        previous_event_hash text,
                        event_hash text not null,
                        created_at text not null,
                        source_session_id text,
                        source_event_id text,
                        blob_refs_json text not null,
                        unique(session_id, sequence)
                    );

                    create unique index idx_session_events_idempotency
                    on session_events(session_id, idempotency_key)
                    where idempotency_key is not null;

                    create index idx_sessions_workspace
                    on sessions(workspace_root, updated_at);

                    create table blobs (
                        content_hash text primary key,
                        content_type text not null,
                        compression text not null,
                        original_size integer not null,
                        stored_size integer not null,
                        data blob not null,
                        created_at text not null
                    );

                    create table event_blob_refs (
                        event_id text not null references session_events(id) on delete cascade,
                        content_hash text not null references blobs(content_hash),
                        role text not null,
                        primary key(event_id, content_hash, role)
                    );
                    """
                )
                connection.execute(
                    "insert into schema_migrations(version, applied_at) values (?, ?)",
                    (DATABASE_SCHEMA_VERSION, _now()),
                )
                connection.execute(f"pragma user_version = {DATABASE_SCHEMA_VERSION}")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, isolation_level=None, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma foreign_keys = on")
        connection.execute("pragma busy_timeout = 5000")
        connection.execute("pragma journal_mode = wal")
        connection.execute("pragma synchronous = full")
        connection.execute("pragma secure_delete = on")
        return connection


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
    return SessionEvent(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        sequence=int(row["sequence"]),
        type=str(row["type"]),
        payload=json.loads(row["payload_json"]),
        schema_version=int(row["schema_version"]),
        created_at=str(row["created_at"]),
        previous_event_hash=row["previous_event_hash"],
        event_hash=str(row["event_hash"]),
        turn_id=row["turn_id"],
        idempotency_key=row["idempotency_key"],
        source_session_id=row["source_session_id"],
        source_event_id=row["source_event_id"],
        blob_refs=_blob_refs_from_json(str(row["blob_refs_json"])),
    )


def _event_hash(**material: Any) -> str:
    encoded = _json_dumps(material).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _normalize_blob_refs(
    references: list[BlobReference] | tuple[BlobReference, ...],
) -> tuple[BlobReference, ...]:
    return tuple(
        sorted(
            set(references),
            key=lambda reference: (reference.role, reference.content_hash),
        )
    )


def _blob_refs_json(references: tuple[BlobReference, ...]) -> str:
    return _json_dumps(
        [
            {"content_hash": reference.content_hash, "role": reference.role}
            for reference in references
        ]
    )


def _blob_refs_from_json(value: str) -> tuple[BlobReference, ...]:
    return tuple(
        BlobReference(content_hash=str(item["content_hash"]), role=str(item["role"]))
        for item in json.loads(value)
    )


def _stored_blob_is_valid(row: sqlite3.Row | None, content_hash: str) -> bool:
    if row is None:
        return False
    stored = bytes(row["data"])
    compression = str(row["compression"])
    try:
        if compression == "zlib":
            raw = zlib.decompress(stored)
        elif compression == "none":
            raw = stored
        else:
            return False
    except zlib.error:
        return False
    return (
        len(stored) == int(row["stored_size"])
        and len(raw) == int(row["original_size"])
        and hashlib.sha256(raw).hexdigest() == content_hash
    )


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
