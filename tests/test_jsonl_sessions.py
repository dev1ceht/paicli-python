from __future__ import annotations

import json
from pathlib import Path

from paicli.session import SessionStore


def test_session_store_persists_and_reopens_current_branch(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(tmp_path / "sessions")

    session = store.create(workspace, title="JSONL session")
    user_id = session.append(
        "message",
        {
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "hello"}],
            }
        },
    )
    assistant_id = session.append(
        "message",
        {
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "hi"}],
            }
        },
    )

    reopened = store.open(session.id)

    assert [entry.id for entry in reopened.current_branch()] == [user_id, assistant_id]
    assert reopened.current_branch()[1].data["message"]["role"] == "assistant"
    lines = session.path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == {
        "type": "session",
        "version": 1,
        "id": session.id,
        "timestamp": session.header.timestamp,
        "cwd": str(workspace.resolve()),
        "parentSession": None,
        "title": "JSONL session",
        "metadata": {},
    }
    assert json.loads(lines[1])["parentId"] is None
    assert json.loads(lines[2])["parentId"] == user_id


def test_session_store_lists_only_workspace_sessions_with_latest_first(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    other_workspace = tmp_path / "other"
    workspace.mkdir()
    other_workspace.mkdir()
    store = SessionStore(tmp_path / "sessions")
    older = store.create(workspace, title="Older")
    newer = store.create(workspace, title="Newer")
    store.create(other_workspace, title="Hidden")
    newer.append("message", {"message": {"role": "user", "content": "latest"}})

    sessions = store.list(workspace)

    assert [session.id for session in sessions] == [newer.id, older.id]
    assert store.latest(workspace).id == newer.id
