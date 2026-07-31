from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from paicli.clock import filename_timestamp, now_timestamp
from paicli.session.manager import SessionHeader, SessionManager, load_session


class SessionStore:
    """Discover and create JSONL Sessions beneath one storage directory."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        workspace_root: str | Path,
        *,
        title: str | None = None,
        parent_session: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> SessionManager:
        workspace = Path(workspace_root).expanduser().resolve()
        session_id = f"sess_{uuid4().hex}"
        header = SessionHeader(
            id=session_id,
            timestamp=now_timestamp(),
            cwd=str(workspace),
            parent_session=parent_session,
            title=title or "New session",
            metadata=dict(metadata or {}),
        )
        directory = self._workspace_directory(workspace)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{filename_timestamp()}_{session_id}.jsonl"
        path.write_text(
            json.dumps(header.to_json(), ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        return SessionManager(path, header)

    def open(self, session_id: str) -> SessionManager:
        for path in self.root.rglob("*.jsonl"):
            session = load_session(path)
            if session.id == session_id:
                return session
        raise KeyError(f"Session not found: {session_id}")

    def list(self, workspace_root: str | Path) -> tuple[SessionManager, ...]:
        workspace = Path(workspace_root).expanduser().resolve()
        directory = self._workspace_directory(workspace)
        if not directory.exists():
            return ()
        sessions = list(self._load_paths(directory.glob("*.jsonl")))
        sessions.sort(
            key=lambda session: (
                session.updated_at,
                session.modified_ns,
                len(session.entries()),
                session.path.name,
            ),
            reverse=True,
        )
        return tuple(sessions)

    def all(self) -> tuple[SessionManager, ...]:
        return self._load_paths(self.root.rglob("*.jsonl"))

    def latest(self, workspace_root: str | Path) -> SessionManager | None:
        sessions = self.list(workspace_root)
        return sessions[0] if sessions else None

    @staticmethod
    def _load_paths(paths) -> tuple[SessionManager, ...]:
        sessions = []
        for path in paths:
            try:
                sessions.append(load_session(path))
            except (KeyError, TypeError, ValueError):
                continue
        return tuple(sessions)

    def _workspace_directory(self, workspace: Path) -> Path:
        digest = hashlib.sha256(str(workspace).casefold().encode("utf-8")).hexdigest()[:16]
        return self.root / digest
