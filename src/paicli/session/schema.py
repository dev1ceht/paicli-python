from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from paicli.session.versions import DATABASE_SCHEMA_VERSION

Migration = Callable[[sqlite3.Connection], None]


def connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, isolation_level=None, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("pragma foreign_keys = on")
    connection.execute("pragma busy_timeout = 5000")
    connection.execute("pragma journal_mode = wal")
    connection.execute("pragma synchronous = full")
    connection.execute("pragma secure_delete = on")
    return connection


def ensure_schema(db_path: Path) -> None:
    with connect(db_path) as connection:
        current = int(connection.execute("pragma user_version").fetchone()[0])
    if current > DATABASE_SCHEMA_VERSION:
        raise RuntimeError(
            f"session database schema {current} is newer than supported {DATABASE_SCHEMA_VERSION}"
        )
    if 0 < current < DATABASE_SCHEMA_VERSION:
        _backup_database(db_path, current)
    for version in range(current + 1, DATABASE_SCHEMA_VERSION + 1):
        migration = _MIGRATIONS[version]
        with connect(db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                migration(connection)
                connection.execute(
                    """
                    insert into schema_migrations(version, applied_at)
                    values (?, ?)
                    """,
                    (version, datetime.now(UTC).isoformat()),
                )
                connection.execute(f"pragma user_version = {version}")
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()


def _backup_database(db_path: Path, current_version: int) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = db_path.with_name(
        f"{db_path.stem}.backup-v{current_version}-{timestamp}{db_path.suffix}"
    )
    with sqlite3.connect(db_path) as source, sqlite3.connect(backup_path) as destination:
        source.backup(destination)
    return backup_path


def _migration_1(connection: sqlite3.Connection) -> None:
    statements = (
        """
        create table if not exists schema_migrations (
            version integer primary key,
            applied_at text not null
        )
        """,
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
        )
        """,
        """
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
        )
        """,
        """
        create unique index idx_session_events_idempotency
        on session_events(session_id, idempotency_key)
        where idempotency_key is not null
        """,
        """
        create index idx_sessions_workspace
        on sessions(workspace_root, updated_at)
        """,
        """
        create table blobs (
            content_hash text primary key,
            content_type text not null,
            compression text not null,
            original_size integer not null,
            stored_size integer not null,
            data blob not null,
            created_at text not null
        )
        """,
        """
        create table event_blob_refs (
            event_id text not null references session_events(id) on delete cascade,
            content_hash text not null references blobs(content_hash),
            role text not null,
            primary key(event_id, content_hash, role)
        )
        """,
    )
    for statement in statements:
        connection.execute(statement)


def _migration_2(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        create table event_blob_refs_v2 (
            event_id text not null references session_events(id) on delete cascade,
            ordinal integer not null,
            content_hash text not null references blobs(content_hash),
            role text not null,
            primary key(event_id, ordinal)
        )
        """
    )
    rows = connection.execute(
        """
        select event_id, content_hash, role
        from event_blob_refs
        order by event_id, rowid
        """
    ).fetchall()
    current_event_id: str | None = None
    ordinal = 0
    for row in rows:
        event_id = str(row["event_id"])
        if event_id != current_event_id:
            current_event_id = event_id
            ordinal = 0
        connection.execute(
            """
            insert into event_blob_refs_v2(event_id, ordinal, content_hash, role)
            values (?, ?, ?, ?)
            """,
            (event_id, ordinal, str(row["content_hash"]), str(row["role"])),
        )
        ordinal += 1
    connection.execute("drop table event_blob_refs")
    connection.execute("alter table event_blob_refs_v2 rename to event_blob_refs")


def _migration_3(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        create table session_leases (
            session_id text primary key references sessions(id) on delete cascade,
            owner_id text not null,
            token text not null unique,
            acquired_at text not null,
            refreshed_at text not null,
            expires_at text not null
        )
        """
    )
    connection.execute(
        """
        create table pending_actions (
            session_id text not null references sessions(id) on delete cascade,
            tool_call_id text not null,
            turn_id text not null,
            tool_name text not null,
            arguments_json text not null,
            raw_call_json text not null,
            status text not null,
            is_read_only integer not null,
            is_idempotent integer not null,
            model_turn integer not null,
            batch_index integer not null,
            approval_status text,
            created_at text not null,
            updated_at text not null,
            primary key(session_id, tool_call_id)
        )
        """
    )
    connection.execute(
        """
        create index idx_pending_actions_session_status
        on pending_actions(session_id, status, model_turn, batch_index)
        """
    )


def _migration_4(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        create table session_roots (
            workspace_root text not null,
            root_kind text not null,
            session_id text not null unique references sessions(id) on delete cascade,
            primary key(workspace_root, root_kind)
        )
        """
    )
    connection.execute(
        """
        create table session_relationships (
            child_session_id text primary key references sessions(id) on delete cascade,
            parent_session_id text not null references sessions(id) on delete cascade,
            relation_type text not null,
            created_at text not null,
            metadata_json text not null
        )
        """
    )
    connection.execute(
        """
        create index idx_session_relationships_parent
        on session_relationships(parent_session_id, created_at, child_session_id)
        """
    )
    connection.execute(
        """
        create table background_tasks (
            id text primary key,
            session_id text not null unique references sessions(id) on delete cascade,
            parent_session_id text not null references sessions(id) on delete cascade,
            queue_session_id text not null references sessions(id) on delete cascade,
            prompt text not null,
            status text not null,
            created_at text not null,
            updated_at text not null,
            started_at text,
            finished_at text,
            result text,
            error text,
            retry_of text references background_tasks(id),
            claim_owner text,
            claim_token text,
            claim_expires_at text
        )
        """
    )
    connection.execute(
        """
        create index idx_background_tasks_queue_status
        on background_tasks(queue_session_id, status, created_at)
        """
    )
    connection.execute(
        """
        create table task_checkpoints (
            task_id text primary key references background_tasks(id) on delete cascade,
            schema_version text not null,
            state_json text not null,
            created_at text not null,
            updated_at text not null
        )
        """
    )
    connection.execute(
        """
        create table task_approvals (
            id text primary key,
            task_id text not null references background_tasks(id) on delete cascade,
            status text not null,
            request_json text not null,
            requested_at text not null,
            decided_at text,
            decision_source text
        )
        """
    )
    connection.execute(
        """
        create index idx_task_approvals_task
        on task_approvals(task_id, requested_at)
        """
    )


_MIGRATIONS: dict[int, Migration] = {
    1: _migration_1,
    2: _migration_2,
    3: _migration_3,
    4: _migration_4,
}
