from __future__ import annotations

from pathlib import Path

from paicli.runtime.task_repository import TaskRepository


def test_task_repository_persists_single_process_lifecycle(tmp_path: Path) -> None:
    repository = TaskRepository(tmp_path / "tasks.db")
    repository.add(
        task_id="task_1",
        workspace_root=str(tmp_path),
        session_id="sess_child",
        parent_session_id="sess_parent",
        prompt="inspect the project",
        retry_of=None,
    )

    claimed = repository.claim_next(str(tmp_path))
    assert claimed is not None
    assert claimed["id"] == "task_1"
    assert claimed["status"] == "running"
    assert repository.transition("task_1", from_status="running", to_status="completed")

    reopened = TaskRepository(tmp_path / "tasks.db")
    assert reopened.get("task_1")["status"] == "completed"  # type: ignore[index]
