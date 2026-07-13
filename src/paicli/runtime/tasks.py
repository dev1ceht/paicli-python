from __future__ import annotations

import json
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
    started_at: str | None = None
    finished_at: str | None = None
    result: str | None = None
    error: str | None = None
    retry_of: str | None = None

    @property
    def duration_seconds(self) -> float | None:
        if not self.started_at:
            return None
        end_at = self.finished_at or (_now() if self.status == "running" else None)
        if not end_at:
            return None
        elapsed = datetime.fromisoformat(end_at) - datetime.fromisoformat(self.started_at)
        return max(0.0, elapsed.total_seconds())

    def to_dict(self) -> dict[str, str | float | None]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "result": self.result,
            "error": self.error,
            "retry_of": self.retry_of,
        }


@dataclass(slots=True)
class TaskApproval:
    id: str
    task_id: str
    status: str
    request: dict[str, object]
    requested_at: str
    decided_at: str | None = None
    decision_source: str | None = None

    def to_dict(self) -> dict[str, object | None]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "status": self.status,
            "request": _redact(self.request),
            "requested_at": self.requested_at,
            "decided_at": self.decided_at,
            "decision_source": self.decision_source,
        }


class DurableTaskManager:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def add(self, prompt: str, *, retry_of: str | None = None) -> str:
        task_id = _new_id("task")
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                insert into tasks(id, prompt, status, created_at, updated_at, retry_of)
                values (?, ?, 'queued', ?, ?, ?)
                """,
                (task_id, prompt, now, now, retry_of),
            )
        return task_id

    def retry(self, task_id: str) -> str | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "select prompt from tasks where id = ? and status = 'failed'", (task_id,)
            ).fetchone()
            if not row:
                return None
            retry_id = _new_id("task")
            now = _now()
            conn.execute(
                """
                insert into tasks(id, prompt, status, created_at, updated_at, retry_of)
                values (?, ?, 'queued', ?, ?, ?)
                """,
                (retry_id, row[0], now, now, task_id),
            )
        return retry_id

    def fail_interrupted_tasks(self) -> int:
        with self._connect() as conn:
            now = _now()
            cursor = conn.execute(
                """
                update tasks
                set status = 'failed', result = null, error = ?, updated_at = ?, finished_at = ?
                where status = 'running'
                """,
                (
                    "Task interrupted by a previous Runtime shutdown; not retried automatically.",
                    now,
                    now,
                ),
            )
            return cursor.rowcount

    def claim_next(self) -> TaskRecord | None:
        with self._connect() as conn:
            # Acquire the SQLite write lock before reading so only this worker can
            # observe and claim the next queued task in this transaction.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                select id, prompt, status, created_at, updated_at, started_at, finished_at, result,
                       error,
                       retry_of
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
                set status = 'running', updated_at = ?, started_at = coalesce(started_at, ?)
                where id = ? and status = 'queued'
                """,
                (updated_at, updated_at, row[0]),
            )
            if cursor.rowcount != 1:
                return None
        return TaskRecord(
            row[0],
            row[1],
            "running",
            row[3],
            updated_at,
            row[5] or updated_at,
            row[6],
            row[7],
            row[8],
            row[9],
        )

    def complete(self, task_id: str, result: str) -> bool:
        return self._update(task_id, "completed", result=result, error=None, from_status="running")

    def fail(self, task_id: str, error: str) -> bool:
        return self._update(task_id, "failed", result=None, error=error, from_status="running")

    def cancel(self, task_id: str) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = _now()
            cursor = conn.execute(
                """
                update tasks
                set status = 'canceled', updated_at = ?, finished_at = ?
                where id = ? and status in ('queued', 'running', 'waiting_approval')
                """,
                (now, now, task_id),
            )
            if cursor.rowcount:
                conn.execute(
                    """
                    update task_approvals
                    set status = 'canceled', decided_at = ?, decision_source = 'cancel'
                    where task_id = ? and status = 'requested'
                    """,
                    (now, task_id),
                )
            return cursor.rowcount > 0

    def wait_for_approval(
        self,
        task_id: str,
        *,
        checkpoint: dict[str, object],
        request: dict[str, object],
        invalidation_reason: str | None = None,
    ) -> TaskApproval | None:
        """Persist an execution checkpoint and move a running task to approval wait."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = _now()
            cursor = conn.execute(
                """
                update tasks
                set status = 'waiting_approval', updated_at = ?
                where id = ? and status = 'running'
                """,
                (now, task_id),
            )
            if cursor.rowcount != 1:
                return None
            if invalidation_reason:
                invalidated_id = _new_id("approval")
                conn.execute(
                    """
                    insert into task_approvals(
                        id, task_id, status, request_json, requested_at, decided_at, decision_source
                    ) values (?, ?, 'invalidated', ?, ?, ?, ?)
                    """,
                    (
                        invalidated_id,
                        task_id,
                        json.dumps(request, ensure_ascii=False),
                        now,
                        now,
                        invalidation_reason,
                    ),
                )
            conn.execute(
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
                (task_id, json.dumps(checkpoint, ensure_ascii=False), now, now),
            )
            approval = TaskApproval(
                id=_new_id("approval"),
                task_id=task_id,
                status="requested",
                request=request,
                requested_at=now,
            )
            conn.execute(
                """
                insert into task_approvals(
                    id, task_id, status, request_json, requested_at, decided_at, decision_source
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval.id,
                    approval.task_id,
                    approval.status,
                    json.dumps(approval.request, ensure_ascii=False),
                    approval.requested_at,
                    approval.decided_at,
                    approval.decision_source,
                ),
            )
            return approval

    def approve(self, task_id: str, *, source: str = "cli") -> bool:
        return self._decide_approval(task_id, decision="approved", source=source)

    def deny(self, task_id: str, *, source: str = "cli") -> bool:
        return self._decide_approval(task_id, decision="denied", source=source)

    def get_checkpoint(self, task_id: str) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute(
                "select state_json from task_checkpoints where task_id = ?", (task_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list_approvals(self, task_id: str) -> list[TaskApproval]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select id, task_id, status, request_json, requested_at, decided_at, decision_source
                from task_approvals
                where task_id = ?
                order by requested_at
                """,
                (task_id,),
            ).fetchall()
        return [
            TaskApproval(
                id=row[0],
                task_id=row[1],
                status=row[2],
                request=json.loads(row[3]),
                requested_at=row[4],
                decided_at=row[5],
                decision_source=row[6],
            )
            for row in rows
        ]

    def list(self, limit: int = 50) -> list[TaskRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select id, prompt, status, created_at, updated_at, started_at, finished_at, result,
                       error,
                       retry_of
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
                select id, prompt, status, created_at, updated_at, started_at, finished_at, result,
                       error,
                       retry_of
                from tasks
                where id = ?
                """,
                (task_id,),
            ).fetchone()
        return TaskRecord(*row) if row else None

    def resolve_reference(self, reference: str, *, limit: int = 20) -> TaskRecord | None:
        value = reference.strip()
        if value == "latest":
            rows = self.list(limit=1)
            return rows[0] if rows else None
        if value.isdecimal():
            index = int(value)
            if index < 1 or index > limit:
                return None
            rows = self.list(limit=limit)
            return rows[index - 1] if index <= len(rows) else None
        return self.get(value)

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
            now = _now()
            cursor = conn.execute(
                """
                update tasks
                set status = ?, result = ?, error = ?, updated_at = ?, finished_at = ?
                where id = ? and status = ?
                """,
                (status, result, error, now, now, task_id, from_status),
            )
            return cursor.rowcount == 1

    def _decide_approval(self, task_id: str, *, decision: str, source: str) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = _now()
            approval = conn.execute(
                """
                select id from task_approvals
                where task_id = ? and status = 'requested'
                order by requested_at desc
                limit 1
                """,
                (task_id,),
            ).fetchone()
            if not approval:
                return False
            task_update = conn.execute(
                """
                update tasks
                set status = 'queued', updated_at = ?
                where id = ? and status = 'waiting_approval'
                """,
                (now, task_id),
            )
            if task_update.rowcount != 1:
                return False
            checkpoint = conn.execute(
                "select state_json from task_checkpoints where task_id = ?", (task_id,)
            ).fetchone()
            if not checkpoint:
                return False
            checkpoint_state = json.loads(checkpoint[0])
            checkpoint_state["approval_decision"] = decision
            conn.execute(
                """
                update task_checkpoints
                set state_json = ?, updated_at = ?
                where task_id = ?
                """,
                (json.dumps(checkpoint_state, ensure_ascii=False), now, task_id),
            )
            approval_update = conn.execute(
                """
                update task_approvals
                set status = ?, decided_at = ?, decision_source = ?
                where id = ? and status = 'requested'
                """,
                (decision, now, source, approval[0]),
            )
            return approval_update.rowcount == 1

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
                    started_at text,
                    finished_at text,
                    result text,
                    error text,
                    retry_of text
                )
                """
            )
            columns = {row[1] for row in conn.execute("pragma table_info(tasks)")}
            if "retry_of" not in columns:
                conn.execute("alter table tasks add column retry_of text")
            conn.execute("create index if not exists idx_tasks_status on tasks(status, created_at)")
            conn.execute(
                """
                create table if not exists task_checkpoints (
                    task_id text primary key,
                    schema_version text not null,
                    state_json text not null,
                    created_at text not null,
                    updated_at text not null
                )
                """
            )
            conn.execute(
                """
                create table if not exists task_approvals (
                    id text primary key,
                    task_id text not null,
                    status text not null,
                    request_json text not null,
                    requested_at text not null,
                    decided_at text,
                    decision_source text
                )
                """
            )
            conn.execute(
                """
                create index if not exists idx_task_approvals_task
                on task_approvals(task_id, requested_at)
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"


def _redact(value: object) -> object:
    sensitive = {"api_key", "authorization", "password", "secret", "token"}
    if isinstance(value, dict):
        return {
            str(key): "***" if str(key).lower() in sensitive else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value
