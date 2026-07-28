from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from paicli.session.models import SessionMessage, SessionRecord
from paicli.session.repository import SessionRepository
from paicli.types import Message, Role


def default_session_database_path() -> Path:
    return Path.home() / ".paicli" / "sessions" / "sessions.db"


class InteractiveSession:
    """Bind one interactive frontend to a durable workspace session."""

    def __init__(
        self,
        repository: SessionRepository,
        workspace_root: str | Path,
        *,
        session_id: str | None = None,
    ) -> None:
        self.repository = repository
        self.workspace_root = str(Path(workspace_root).expanduser().resolve())
        self.record = self._open_session(session_id)
        self._active_turn_id: str | None = None

    @property
    def id(self) -> str:
        return self.record.id

    @property
    def session_history(self) -> tuple[SessionMessage, ...]:
        return self.repository.rebuild_session_view(self.id).session_history

    def restore_agent_history(self, agent: Any) -> None:
        view = self.repository.rebuild_session_view(self.id)
        agent.replace_history(
            [
                Message(role=cast(Role, message.role), content=message.content)
                for message in view.model_messages
            ]
        )

    def begin_turn(self, message: str) -> str:
        if self._active_turn_id is not None:
            raise RuntimeError("session already has an active turn")
        turn_id = f"turn_{uuid4().hex}"
        self.repository.begin_turn(
            self.id,
            turn_id=turn_id,
            user_content=message,
        )
        self._active_turn_id = turn_id
        return turn_id

    def complete_turn(self, assistant_text: str) -> None:
        turn_id = self._require_active_turn()
        self.repository.complete_turn(
            self.id,
            turn_id=turn_id,
            assistant_content=assistant_text,
        )
        self._active_turn_id = None

    def interrupt_turn(self, assistant_text: str, *, reason: str) -> None:
        if self._active_turn_id is None:
            return
        turn_id = self._active_turn_id
        self.repository.interrupt_turn(
            self.id,
            turn_id=turn_id,
            assistant_content=assistant_text,
            reason=reason,
        )
        self._active_turn_id = None

    def reset_context(self) -> None:
        if self._active_turn_id is not None:
            raise RuntimeError("cannot reset context during an active turn")
        self.repository.reset_context(self.id)

    def list_sessions(self) -> tuple[SessionRecord, ...]:
        return tuple(
            record
            for record in self.repository.list_sessions(
                include_archived=True,
                include_deleted=True,
            )
            if record.workspace_root == self.workspace_root
        )

    def new_session(self, *, title: str | None = None) -> SessionRecord:
        self._require_idle()
        self.record = self.repository.create_session(
            self.workspace_root,
            title=title,
        )
        return self.record

    def resume_session(self, session_id: str) -> SessionRecord:
        self._require_idle()
        record = self._resolve_workspace_session(session_id, allow_archived=True)
        if record.archived_at is not None:
            record = self.repository.unarchive_session(session_id)
        self.record = record
        return self.record

    def fork_session(self, *, title: str | None = None) -> SessionRecord:
        self._require_idle()
        self.record = self.repository.fork_session(
            self.id,
            workspace_root=self.workspace_root,
            title=title,
        )
        return self.record

    def archive_session(self) -> tuple[SessionRecord, SessionRecord]:
        self._require_idle()
        archived = self.repository.archive_session(self.id)
        replacement = self.repository.create_session(self.workspace_root)
        self.record = replacement
        return archived, replacement

    def delete_session(self) -> tuple[SessionRecord, SessionRecord]:
        self._require_idle()
        deleted = self.repository.delete_session(self.id)
        replacement = self.repository.create_session(self.workspace_root)
        self.record = replacement
        return deleted, replacement

    def restore_session(self, session_id: str) -> SessionRecord:
        self._require_idle()
        record = self._resolve_workspace_session(
            session_id,
            allow_archived=True,
            allow_deleted=True,
        )
        if record.deleted_at is None:
            raise ValueError(f"session is not deleted: {session_id}")
        self.record = self.repository.restore_session(session_id)
        return self.record

    def _require_active_turn(self) -> str:
        if self._active_turn_id is None:
            raise RuntimeError("session has no active turn")
        return self._active_turn_id

    def _require_idle(self) -> None:
        if self._active_turn_id is not None:
            raise RuntimeError("cannot switch sessions during an active turn")

    def _open_session(self, session_id: str | None) -> SessionRecord:
        if session_id is not None:
            return self._resolve_workspace_session(session_id)
        existing = next(
            (
                record
                for record in self.repository.list_sessions()
                if record.workspace_root == self.workspace_root and record.status != "corrupt"
            ),
            None,
        )
        return existing or self.repository.create_session(self.workspace_root)

    def _resolve_workspace_session(
        self,
        session_id: str,
        *,
        allow_archived: bool = False,
        allow_deleted: bool = False,
    ) -> SessionRecord:
        record = self.repository.get_session(session_id)
        if record is None:
            raise KeyError(f"session not found: {session_id}")
        if record.workspace_root != self.workspace_root:
            raise ValueError(f"session belongs to another workspace: {session_id}")
        if record.status == "corrupt":
            raise ValueError(f"session is corrupt: {session_id}")
        if record.archived_at is not None and not allow_archived:
            raise ValueError(f"session is archived: {session_id}")
        if record.deleted_at is not None and not allow_deleted:
            raise ValueError(f"session is deleted: {session_id}")
        return record
