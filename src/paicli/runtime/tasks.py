from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from paicli.clock import filename_timestamp, now_timestamp, parse_timestamp
from paicli.runtime.task_repository import TaskRepository
from paicli.session import SessionRepository, default_session_directory


def default_task_database_path() -> Path:
    return Path.home() / ".paicli" / "runtime" / "tasks.db"


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
        end_at = self.finished_at or (now_timestamp() if self.status == "running" else None)
        if not end_at:
            return None
        return max(
            0.0, (parse_timestamp(end_at) - parse_timestamp(self.started_at)).total_seconds()
        )

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
    """Coordinate Background tasks in SQLite and execution history in child Sessions."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        session_repository: SessionRepository | None = None,
        workspace_root: str | Path | None = None,
        parent_session_id: str | None = None,
    ) -> None:
        self.task_repository = TaskRepository(db_path)
        self.db_path = self.task_repository.db_path
        if session_repository is None:
            default_tasks = default_task_database_path().expanduser().resolve()
            session_root = (
                default_session_directory()
                if self.db_path.resolve() == default_tasks
                else self.db_path.parent / "sessions"
            )
            session_repository = SessionRepository(session_root)
        self.repository = session_repository
        self.workspace_root = str(
            Path(workspace_root or self.db_path.parent).expanduser().resolve()
        )
        self.parent_session_id = parent_session_id or self._resolve_task_root()

    def add(self, prompt: str, *, retry_of: str | None = None) -> str:
        parent_session_id = self.parent_session_id
        relation_type = "background_task"
        if retry_of is not None:
            source = self.get(retry_of)
            if source is None:
                raise KeyError(f"retry source task not found: {retry_of}")
            parent_session_id = source.session_id
            relation_type = "background_task_retry"
        task_id = _new_id("task")
        child = self.repository.create_child_session(
            parent_session_id,
            relation_type=relation_type,
            title=prompt[:80] or task_id,
            metadata={"session_kind": "background_task", "task_id": task_id},
        )
        self.task_repository.add(
            task_id=task_id,
            workspace_root=self.workspace_root,
            session_id=child.id,
            parent_session_id=parent_session_id,
            prompt=prompt,
            retry_of=retry_of,
        )
        self.repository.append_event(
            child.id,
            "background_task.queued",
            {"task_id": task_id, "prompt": prompt, "retry_of": retry_of},
        )
        return task_id

    def retry(self, task_id: str) -> str | None:
        task = self.get(task_id)
        if task is None or task.status != "failed":
            return None
        return self.add(task.prompt, retry_of=task_id)

    def fail_interrupted_tasks(self) -> int:
        error = "Task interrupted by a previous Runtime shutdown; not retried automatically."
        interrupted = self.task_repository.fail_running(
            self.workspace_root,
            error,
        )
        for row in interrupted:
            task = _task_from_row(row)
            self.repository.interrupt_active_turn(
                task.session_id,
                reason="process_restarted",
            )
            self.repository.append_event(
                task.session_id,
                "background_task.failed",
                {"task_id": task.id, "error": error},
            )
        return len(interrupted)

    def claim_next(self) -> TaskRecord | None:
        row = self.task_repository.claim_next(self.workspace_root)
        if row is None:
            return None
        task = _task_from_row(row)
        self.repository.append_event(
            task.session_id,
            "background_task.running",
            {"task_id": task.id},
        )
        return task

    def complete(self, task_id: str, result: str) -> bool:
        updated = self.task_repository.transition(
            task_id,
            from_status="running",
            to_status="completed",
            result=result,
        )
        if updated:
            self._record_status(task_id, "completed", {"result": result})
        return updated

    def fail(self, task_id: str, error: str) -> bool:
        updated = self.task_repository.transition(
            task_id,
            from_status="running",
            to_status="failed",
            error=error,
        )
        if updated:
            self._record_status(task_id, "failed", {"error": error})
        return updated

    def cancel(self, task_id: str) -> bool:
        task = self.get(task_id)
        if task is None or task.status in {"completed", "failed", "canceled"}:
            return False
        updated = self.task_repository.transition(
            task_id,
            from_status=task.status,
            to_status="canceled",
        )
        if not updated:
            return False
        self.repository.interrupt_active_turn(
            task.session_id,
            reason="background_task_canceled",
        )
        self._record_status(task_id, "canceled")
        return True

    def wait_for_approval(
        self,
        task_id: str,
        *,
        checkpoint: dict[str, object],
        request: dict[str, object],
        invalidation_reason: str | None = None,
        **_: object,
    ) -> TaskApproval | None:
        if invalidation_reason:
            checkpoint = {**checkpoint, "approval_invalidation_reason": invalidation_reason}
        approval_id = _new_id("approval")
        if not self.task_repository.wait_for_approval(
            task_id,
            approval_id=approval_id,
            checkpoint=checkpoint,
            request=request,
            invalidation_reason=invalidation_reason,
        ):
            return None
        return next(
            approval for approval in self.list_approvals(task_id) if approval.id == approval_id
        )

    def approve(self, task_id: str, *, source: str = "cli") -> bool:
        return self.task_repository.decide_approval(
            task_id,
            decision="approved",
            source=source,
        )

    def deny(self, task_id: str, *, source: str = "cli") -> bool:
        return self.task_repository.decide_approval(
            task_id,
            decision="denied",
            source=source,
        )

    def get_checkpoint(self, task_id: str) -> dict[str, object] | None:
        return self.task_repository.checkpoint(task_id)

    def list_approvals(self, task_id: str) -> list[TaskApproval]:
        return [
            TaskApproval(
                id=str(row["id"]),
                task_id=str(row["task_id"]),
                status=str(row["status"]),
                request=dict(row["request"]),
                requested_at=str(row["requested_at"]),
                decided_at=(str(row["decided_at"]) if row["decided_at"] else None),
                decision_source=(str(row["decision_source"]) if row["decision_source"] else None),
            )
            for row in self.task_repository.approvals(task_id)
        ]

    def list(self, limit: int = 50) -> list[TaskRecord]:
        return [
            _task_from_row(row)
            for row in self.task_repository.list(self.workspace_root, limit=limit)
        ]

    def get(self, task_id: str) -> TaskRecord | None:
        row = self.task_repository.get(task_id)
        if row is None or row["workspace_root"] != self.workspace_root:
            return None
        return _task_from_row(row)

    def resolve_reference(self, reference: str, *, limit: int = 20) -> TaskRecord | None:
        value = reference.strip()
        if value == "latest":
            tasks = self.list(limit=1)
            return tasks[0] if tasks else None
        if value.isdecimal():
            index = int(value)
            tasks = self.list(limit=limit)
            return tasks[index - 1] if 1 <= index <= len(tasks) else None
        return self.get(value)

    def _resolve_task_root(self) -> str:
        return self.repository.get_or_create_root_session(
            self.workspace_root,
            title="Background tasks",
            root_kind="background_task_root",
        ).id

    def _record_status(
        self,
        task_id: str,
        status: str,
        details: dict[str, object] | None = None,
    ) -> None:
        task = self.get(task_id)
        if task is None:
            return
        self.repository.append_event(
            task.session_id,
            f"background_task.{status}",
            {"task_id": task_id, **dict(details or {})},
        )


def _task_from_row(row: dict[str, object]) -> TaskRecord:
    return TaskRecord(
        id=str(row["id"]),
        prompt=str(row["prompt"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        started_at=_optional_string(row.get("started_at")),
        finished_at=_optional_string(row.get("finished_at")),
        result=_optional_string(row.get("result")),
        error=_optional_string(row.get("error")),
        retry_of=_optional_string(row.get("retry_of")),
        session_id=str(row["session_id"]),
        parent_session_id=str(row["parent_session_id"]),
    )


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


def _new_id(prefix: str) -> str:
    return f"{prefix}_{filename_timestamp()}_{uuid4().hex[:8]}"


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
