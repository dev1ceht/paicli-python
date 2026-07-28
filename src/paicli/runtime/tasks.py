from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from paicli.session import SessionRepository
from paicli.session.schema import connect


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
    session_id: str = ""
    parent_session_id: str = ""

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
            "session_id": self.session_id,
            "parent_session_id": self.parent_session_id,
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
    def __init__(
        self,
        db_path: str | Path | SessionRepository,
        *,
        workspace_root: str | Path | None = None,
        parent_session_id: str | None = None,
        queue_session_id: str | None = None,
        parent_lease_token: str | None = None,
        claim_ttl_seconds: int = 60,
    ):
        self.repository = (
            db_path if isinstance(db_path, SessionRepository) else SessionRepository(db_path)
        )
        self.db_path = self.repository.db_path
        self.workspace_root = str(
            Path(workspace_root or self.db_path.parent).expanduser().resolve()
        )
        self.parent_session_id = parent_session_id or self._resolve_task_root()
        self.queue_session_id = queue_session_id or self._resolve_runtime_queue()
        self.parent_lease_token = parent_lease_token
        self.claim_ttl_seconds = claim_ttl_seconds
        self._claim_owner = f"task_manager_{uuid4().hex}"
        self._claim_tokens: dict[str, str] = {}
        self._claim_tokens_lock = threading.Lock()

    def add(self, prompt: str, *, retry_of: str | None = None) -> str:
        task_id = _new_id("task")
        parent_session_id = self.parent_session_id
        relation_type = "background_task"
        if retry_of is not None:
            retried = self.get(retry_of)
            if retried is None:
                raise KeyError(f"retry source task not found: {retry_of}")
            parent_session_id = retried.session_id
            relation_type = "background_task_retry"
        self.repository.create_background_task(
            parent_session_id,
            queue_session_id=self.queue_session_id,
            task_id=task_id,
            prompt=prompt,
            retry_of=retry_of,
            relation_type=relation_type,
            lease_token=(
                self.parent_lease_token if parent_session_id == self.parent_session_id else None
            ),
        )
        return task_id

    def retry(self, task_id: str) -> str | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                select prompt
                from background_tasks
                where id = ? and queue_session_id = ? and status = 'failed'
                """,
                (task_id, self.queue_session_id),
            ).fetchone()
            if not row:
                return None
        return self.add(str(row[0]), retry_of=task_id)

    def fail_interrupted_tasks(self) -> int:
        return self.repository.fail_interrupted_background_tasks(
            self.queue_session_id,
            "Task interrupted by a previous Runtime shutdown; not retried automatically."
        )

    def claim_next(self) -> TaskRecord | None:
        self.fail_interrupted_tasks()
        row = self.repository.claim_next_background_task(
            self.queue_session_id,
            owner_id=self._claim_owner,
            ttl_seconds=self.claim_ttl_seconds,
        )
        if row is None:
            return None
        claim_token = str(row.pop("claim_token"))
        task = TaskRecord(**row)
        with self._claim_tokens_lock:
            self._claim_tokens[task.id] = claim_token
        return task

    def refresh_claim(self, task_id: str) -> None:
        with self._claim_tokens_lock:
            claim_token = self._claim_tokens.get(task_id)
        if claim_token is None or not self.repository.refresh_background_task_claim(
            task_id,
            owner_id=self._claim_owner,
            claim_token=claim_token,
            ttl_seconds=self.claim_ttl_seconds,
        ):
            raise RuntimeError(f"background task claim is no longer owned: {task_id}")

    def complete(self, task_id: str, result: str) -> bool:
        return self._update(task_id, "completed", result=result, error=None, from_status="running")

    def fail(self, task_id: str, error: str) -> bool:
        return self._update(task_id, "failed", result=None, error=error, from_status="running")

    def cancel(self, task_id: str) -> bool:
        canceled = self.repository.cancel_background_task(
            task_id,
            queue_session_id=self.queue_session_id,
        )
        if canceled:
            self._forget_claim(task_id)
        return canceled

    def wait_for_approval(
        self,
        task_id: str,
        *,
        checkpoint: dict[str, object],
        request: dict[str, object],
        invalidation_reason: str | None = None,
        session_id: str | None = None,
        tool_call_id: str | None = None,
        lease_token: str | None = None,
    ) -> TaskApproval | None:
        """Persist an execution checkpoint and move a running task to approval wait."""
        claim_token = self._claim_token(task_id)
        if claim_token is None:
            return None
        if session_id is not None and tool_call_id is not None:
            approval_id = _new_id("approval")
            if not self.repository.pause_background_task_for_approval(
                task_id,
                session_id=session_id,
                tool_call_id=tool_call_id,
                checkpoint=checkpoint,
                approval_id=approval_id,
                request=request,
                claim_owner=self._claim_owner,
                claim_token=claim_token,
                invalidation_reason=invalidation_reason,
                lease_token=lease_token,
            ):
                return None
            return next(
                approval
                for approval in reversed(self.list_approvals(task_id))
                if approval.id == approval_id
            )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = _now()
            cursor = conn.execute(
                """
                update background_tasks
                set status = 'waiting_approval', updated_at = ?
                where id = ? and status = 'running'
                  and claim_owner = ? and claim_token = ? and claim_expires_at > ?
                """,
                (now, task_id, self._claim_owner, claim_token, now),
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
        if self.get(task_id) is None:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "select state_json from task_checkpoints where task_id = ?", (task_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list_approvals(self, task_id: str) -> list[TaskApproval]:
        if self.get(task_id) is None:
            return []
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
                       error, retry_of, session_id, parent_session_id
                from background_tasks
                where queue_session_id = ?
                order by created_at desc
                limit ?
                """,
                (self.queue_session_id, limit),
            ).fetchall()
        return [TaskRecord(*row) for row in rows]

    def get(self, task_id: str) -> TaskRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select id, prompt, status, created_at, updated_at, started_at, finished_at, result,
                       error, retry_of, session_id, parent_session_id
                from background_tasks
                where id = ? and queue_session_id = ?
                """,
                (task_id, self.queue_session_id),
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
        claim_token = self._claim_token(task_id)
        if claim_token is None:
            return False
        updated = self.repository.transition_background_task(
            task_id,
            status=status,
            from_status=from_status,
            result=result,
            error=error,
            claim_owner=self._claim_owner,
            claim_token=claim_token,
        )
        if updated:
            self._forget_claim(task_id)
        return updated

    def _forget_claim(self, task_id: str) -> None:
        with self._claim_tokens_lock:
            self._claim_tokens.pop(task_id, None)

    def _claim_token(self, task_id: str) -> str | None:
        with self._claim_tokens_lock:
            return self._claim_tokens.get(task_id)

    def _decide_approval(self, task_id: str, *, decision: str, source: str) -> bool:
        if self.get(task_id) is None:
            return False
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
                update background_tasks
                set status = 'queued', updated_at = ?,
                    claim_owner = null, claim_token = null, claim_expires_at = null
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
            decided = approval_update.rowcount == 1
        if decided:
            self._forget_claim(task_id)
        return decided

    def _connect(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def _resolve_task_root(self) -> str:
        return self.repository.get_or_create_root_session(
            self.workspace_root,
            title="Background tasks",
            root_kind="background_task_root",
        ).id

    def _resolve_runtime_queue(self) -> str:
        return self.repository.get_or_create_root_session(
            self.workspace_root,
            title="Runtime",
            root_kind="runtime_root",
        ).id


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
