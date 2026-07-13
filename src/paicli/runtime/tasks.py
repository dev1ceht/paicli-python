from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(slots=True)
class TaskRecord:
    id: str
    prompt: str
    status: str
    created_at: str
    updated_at: str
    result: str | None = None
    error: str | None = None


class DurableTaskManager:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def add(self, prompt: str) -> str:
        task_id = _new_id("task")
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                insert into tasks(id, prompt, status, created_at, updated_at)
                values (?, ?, 'queued', ?, ?)
                """,
                (task_id, prompt, now, now),
            )
        return task_id

    def claim_next(self) -> TaskRecord | None:
        with self._connect() as conn:
            # Acquire the SQLite write lock before reading so only this worker can
            # observe and claim the next queued task in this transaction.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                select id, prompt, status, created_at, updated_at, result, error
                from tasks
                where status = 'queued'
                order by created_at
                limit 1
                """
            ).fetchone()
            if not row:
                return None
            updated_at = _now()
            cursor = conn.execute(
                """
                update tasks
                set status = 'running', updated_at = ?
                where id = ? and status = 'queued'
                """,
                (updated_at, row[0]),
            )
            if cursor.rowcount != 1:
                return None
        return TaskRecord(*row[:2], "running", row[3], updated_at, row[5], row[6])

    def complete(self, task_id: str, result: str) -> bool:
        return self._update(task_id, "completed", result=result, error=None, from_status="running")

    def fail(self, task_id: str, error: str) -> bool:
        return self._update(task_id, "failed", result=None, error=error, from_status="running")

    def cancel(self, task_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                update tasks
                set status = 'canceled', updated_at = ?
                where id = ? and status in ('queued', 'running')
                """,
                (_now(), task_id),
            )
            return cursor.rowcount > 0

    def list(self, limit: int = 50) -> list[TaskRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select id, prompt, status, created_at, updated_at, result, error
                from tasks
                order by created_at desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [TaskRecord(*row) for row in rows]

    def get(self, task_id: str) -> TaskRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select id, prompt, status, created_at, updated_at, result, error
                from tasks
                where id = ?
                """,
                (task_id,),
            ).fetchone()
        return TaskRecord(*row) if row else None

    def _update(
        self,
        task_id: str,
        status: str,
        *,
        result: str | None,
        error: str | None,
        from_status: str,
    ) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                update tasks
                set status = ?, result = ?, error = ?, updated_at = ?
                where id = ? and status = ?
                """,
                (status, result, error, _now(), task_id, from_status),
            )
            return cursor.rowcount == 1

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists tasks (
                    id text primary key,
                    prompt text not null,
                    status text not null,
                    created_at text not null,
                    updated_at text not null,
                    result text,
                    error text
                )
                """
            )
            conn.execute("create index if not exists idx_tasks_status on tasks(status, created_at)")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"
