from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from paicli.clock import now_timestamp


class TaskRepository:
    """SQLite-backed Background-task lifecycle for one local process."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def add(
        self,
        *,
        task_id: str,
        workspace_root: str,
        session_id: str,
        parent_session_id: str,
        prompt: str,
        retry_of: str | None,
    ) -> None:
        now = now_timestamp()
        with self._connect() as connection:
            connection.execute(
                """
                insert into background_tasks(
                    id, workspace_root, session_id, parent_session_id, prompt,
                    status, created_at, updated_at, retry_of
                ) values (?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    task_id,
                    workspace_root,
                    session_id,
                    parent_session_id,
                    prompt,
                    now,
                    now,
                    retry_of,
                ),
            )

    def claim_next(self, workspace_root: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                select * from background_tasks
                where workspace_root = ? and status = 'queued'
                order by created_at, rowid
                limit 1
                """,
                (workspace_root,),
            ).fetchone()
            if row is None:
                return None
            now = now_timestamp()
            connection.execute(
                """
                update background_tasks
                set status = 'running', started_at = ?, updated_at = ?
                where id = ?
                """,
                (now, now, row["id"]),
            )
            return self.get(str(row["id"]), connection=connection)

    def transition(
        self,
        task_id: str,
        *,
        from_status: str,
        to_status: str,
        result: str | None = None,
        error: str | None = None,
    ) -> bool:
        now = now_timestamp()
        finished_at = now if to_status in {"completed", "failed", "canceled"} else None
        with self._connect() as connection:
            cursor = connection.execute(
                """
                update background_tasks
                set status = ?, updated_at = ?, finished_at = ?, result = ?, error = ?
                where id = ? and status = ?
                """,
                (to_status, now, finished_at, result, error, task_id, from_status),
            )
            return cursor.rowcount == 1

    def get(
        self,
        task_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        if connection is not None:
            row = connection.execute(
                "select * from background_tasks where id = ?",
                (task_id,),
            ).fetchone()
            return dict(row) if row is not None else None
        with self._connect() as owned:
            return self.get(task_id, connection=owned)

    def list(self, workspace_root: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select * from background_tasks
                where workspace_root = ?
                order by created_at desc, rowid desc
                limit ?
                """,
                (workspace_root, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def fail_running(self, workspace_root: str, error: str) -> list[dict[str, Any]]:
        now = now_timestamp()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                select * from background_tasks
                where workspace_root = ? and status = 'running'
                order by created_at, rowid
                """,
                (workspace_root,),
            ).fetchall()
            connection.execute(
                """
                update background_tasks
                set status = 'failed', updated_at = ?, finished_at = ?, error = ?
                where workspace_root = ? and status = 'running'
                """,
                (now, now, error, workspace_root),
            )
            return [dict(row) for row in rows]

    def wait_for_approval(
        self,
        task_id: str,
        *,
        approval_id: str,
        checkpoint: dict[str, object],
        request: dict[str, object],
        invalidation_reason: str | None = None,
    ) -> bool:
        now = now_timestamp()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                update background_tasks
                set status = 'waiting_approval', updated_at = ?
                where id = ? and status = 'running'
                """,
                (now, task_id),
            )
            if cursor.rowcount != 1:
                return False
            if invalidation_reason:
                connection.execute(
                    """
                    insert into task_approvals(
                        id, task_id, status, request_json, requested_at,
                        decided_at, decision_source
                    ) values (?, ?, 'invalidated', ?, ?, ?, ?)
                    """,
                    (
                        f"{approval_id}_invalidated",
                        task_id,
                        json.dumps(request, ensure_ascii=False),
                        now,
                        now,
                        invalidation_reason,
                    ),
                )
            connection.execute(
                """
                insert into task_checkpoints(task_id, schema_version, state_json, updated_at)
                values (?, 'approval-v1', ?, ?)
                on conflict(task_id) do update set
                    schema_version = excluded.schema_version,
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (task_id, json.dumps(checkpoint, ensure_ascii=False), now),
            )
            connection.execute(
                """
                insert into task_approvals(
                    id, task_id, status, request_json, requested_at
                ) values (?, ?, 'requested', ?, ?)
                """,
                (approval_id, task_id, json.dumps(request, ensure_ascii=False), now),
            )
        return True

    def decide_approval(self, task_id: str, *, decision: str, source: str) -> bool:
        now = now_timestamp()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            approval = connection.execute(
                """
                select id from task_approvals
                where task_id = ? and status = 'requested'
                order by requested_at desc, rowid desc limit 1
                """,
                (task_id,),
            ).fetchone()
            if approval is None:
                return False
            checkpoint = connection.execute(
                "select state_json from task_checkpoints where task_id = ?",
                (task_id,),
            ).fetchone()
            if checkpoint is None:
                return False
            state = json.loads(str(checkpoint["state_json"]))
            state["approval_decision"] = decision
            task_cursor = connection.execute(
                """
                update background_tasks set status = 'queued', updated_at = ?
                where id = ? and status = 'waiting_approval'
                """,
                (now, task_id),
            )
            if task_cursor.rowcount != 1:
                return False
            connection.execute(
                "update task_checkpoints set state_json = ?, updated_at = ? where task_id = ?",
                (json.dumps(state, ensure_ascii=False), now, task_id),
            )
            connection.execute(
                """
                update task_approvals
                set status = ?, decided_at = ?, decision_source = ?
                where id = ?
                """,
                (decision, now, source, approval["id"]),
            )
        return True

    def checkpoint(self, task_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "select state_json from task_checkpoints where task_id = ?",
                (task_id,),
            ).fetchone()
        return json.loads(str(row["state_json"])) if row is not None else None

    def approvals(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select * from task_approvals
                where task_id = ? order by requested_at, rowid
                """,
                (task_id,),
            ).fetchall()
        return [{**dict(row), "request": json.loads(str(row["request_json"]))} for row in rows]

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                create table if not exists background_tasks (
                    id text primary key,
                    workspace_root text not null,
                    session_id text not null,
                    parent_session_id text not null,
                    prompt text not null,
                    status text not null,
                    created_at text not null,
                    updated_at text not null,
                    started_at text,
                    finished_at text,
                    result text,
                    error text,
                    retry_of text references background_tasks(id)
                );
                create index if not exists idx_background_tasks_queue
                on background_tasks(workspace_root, status, created_at);
                create table if not exists task_checkpoints (
                    task_id text primary key references background_tasks(id) on delete cascade,
                    schema_version text not null,
                    state_json text not null,
                    updated_at text not null
                );
                create table if not exists task_approvals (
                    id text primary key,
                    task_id text not null references background_tasks(id) on delete cascade,
                    status text not null,
                    request_json text not null,
                    requested_at text not null,
                    decided_at text,
                    decision_source text
                );
                create index if not exists idx_task_approvals_task
                on task_approvals(task_id, requested_at);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma foreign_keys = on")
        return connection
