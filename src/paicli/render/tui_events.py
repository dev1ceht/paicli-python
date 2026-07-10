from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class UiEvent:
    kind: str
    payload: dict[str, Any]
    task_id: str | None = None

    @classmethod
    def from_agent(cls, event: dict[str, Any]) -> "UiEvent":
        task_id = event.get("task_id")
        if task_id is None:
            task = event.get("task")
            if isinstance(task, dict):
                raw_task_id = task.get("id")
                task_id = str(raw_task_id) if raw_task_id is not None else None
        return cls(
            kind=str(event.get("type") or "unknown"),
            payload=dict(event),
            task_id=str(task_id) if task_id is not None else None,
        )
