from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Protocol


@dataclass(slots=True)
class SnapshotRecord:
    """A workspace checkpoint; it contains no conversation/session identity."""

    id: str
    phase: str
    created_at: str
    path: Path
    backend: str = "side-git"


class WorkspaceCheckpointStore(Protocol):
    """Small seam shared by Git and non-Git workspace checkpoint adapters."""

    def create(self, phase: str) -> SnapshotRecord: ...

    def list(self, limit: int = 20) -> list[SnapshotRecord]: ...

    def restore(self, snapshot_ref: str) -> SnapshotRecord: ...

    def clean(self) -> int: ...

    def status(self) -> str: ...


class WorkspaceCheckpointCoordinator:
    """Create at most one automatic checkpoint for a workspace mutation scope."""

    def __init__(self, store: WorkspaceCheckpointStore | None):
        self.store = store
        self._record: SnapshotRecord | None = None
        self._lock = Lock()

    def ensure(self) -> SnapshotRecord | None:
        if self.store is None:
            return None
        if self._record is None:
            with self._lock:
                if self._record is None:
                    self._record = self.store.create("before-agent-edit")
        return self._record

    @property
    def record(self) -> SnapshotRecord | None:
        return self._record
