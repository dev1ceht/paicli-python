from __future__ import annotations

from pathlib import Path

import pytest

from paicli.session import SessionRepository, ToolActionSpec


def test_repository_creates_jsonl_session_and_replays_messages(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions")

    session = repository.create_session(workspace, title="Persistent work")
    repository.begin_turn(session.id, turn_id="turn_1", user_content="hello")
    repository.complete_turn(
        session.id,
        turn_id="turn_1",
        assistant_content="hi",
        reasoning_content="considered the greeting",
        reasoning_duration=0.25,
    )

    assert not (tmp_path / "sessions.db").exists()
    assert len(list((tmp_path / "sessions").rglob("*.jsonl"))) == 1
    view = repository.rebuild_session_view(session.id)
    assert [(message.role, message.content) for message in view.model_messages] == [
        ("user", "hello"),
        ("assistant", "hi"),
    ]
    assert view.model_messages[-1].parts[0].metadata["reasoning_content"] == (
        "considered the greeting"
    )
    assert view.model_messages[-1].parts[0].metadata["reasoning_duration"] == 0.25


def test_repository_persists_thinking_only_interruption(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    session = repository.create_session(tmp_path)
    repository.begin_turn(session.id, turn_id="turn_1", user_content="think")

    repository.interrupt_turn(
        session.id,
        turn_id="turn_1",
        assistant_content="",
        reasoning_content="unfinished reasoning",
        reason="user_interrupt",
    )

    message = repository.rebuild_session_view(session.id).session_history[-1]
    assert message.role == "assistant"
    assert message.status == "partial"
    assert message.content == ""
    assert message.parts[0].metadata["reasoning_content"] == "unfinished reasoning"


def test_repository_keeps_large_content_inline_in_session_file(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    session = repository.create_session(tmp_path)
    content = "界" * 70_000

    repository.append_message(session.id, role="user", content=content)

    path = repository.store.open(session.id).path
    assert content in path.read_text(encoding="utf-8")
    assert repository.rebuild_session_view(session.id).model_messages[0].content == content


def test_repository_projects_session_lifecycle_from_jsonl(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    session = repository.create_session(tmp_path, title="Original")

    renamed = repository.update_session_metadata(session.id, title="Renamed")
    archived = repository.archive_session(session.id)
    unarchived = repository.unarchive_session(session.id)
    deleted = repository.delete_session(session.id)
    restored = repository.restore_session(session.id)

    assert renamed.title == "Renamed"
    assert archived.archived_at is not None
    assert unarchived.archived_at is None
    assert deleted.deleted_at is not None
    assert restored.deleted_at is None


def test_repository_rebuilds_pending_tool_action_state_from_entries(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    session = repository.create_session(tmp_path)
    repository.begin_turn(session.id, turn_id="turn_tool", user_content="inspect")
    repository.prepare_tool_actions(
        session.id,
        turn_id="turn_tool",
        model_turn=1,
        assistant_content="",
        actions=(
            ToolActionSpec(
                tool_call_id="call_1",
                tool_name="read",
                arguments={"path": "README.md"},
                raw_call={"id": "call_1", "function": {"name": "read"}},
                is_read_only=True,
                is_idempotent=True,
            ),
        ),
    )
    repository.request_tool_approval(session.id, "call_1")
    repository.resolve_tool_approval(session.id, "call_1", decision="approve")
    repository.complete_tool_action(
        session.id,
        "call_1",
        content="contents",
        is_error=False,
    )

    action = repository.list_pending_actions(session.id, include_settled=True)[0]
    assert action.status == "completed"
    assert action.approval_status == "approve"
    assert repository.rebuild_session_view(session.id).model_messages[-1].content == "contents"


def test_repository_fork_creates_child_jsonl_with_replayable_history(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    parent = repository.create_session(tmp_path, title="Parent")
    repository.begin_turn(parent.id, turn_id="turn_1", user_content="question")
    repository.complete_turn(parent.id, turn_id="turn_1", assistant_content="answer")

    child = repository.fork_session(parent.id, title="Child")

    relationship = repository.get_parent_relationship(child.id)
    assert relationship is not None
    assert relationship.parent_session_id == parent.id
    assert [
        message.content for message in repository.rebuild_session_view(child.id).model_messages
    ] == [
        "question",
        "answer",
    ]


def test_repository_ignores_incomplete_trailing_jsonl_line(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    session = repository.create_session(tmp_path)
    repository.append_message(session.id, role="user", content="survives")
    path = repository.store.open(session.id).path
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"type":"message"')

    reopened = SessionRepository(tmp_path / "sessions")

    assert reopened.rebuild_session_view(session.id).model_messages[0].content == "survives"


def test_repository_rejects_malformed_interior_jsonl_line(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    session = repository.create_session(tmp_path)
    repository.append_message(session.id, role="user", content="before corruption")
    path = repository.store.open(session.id).path
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"type":\n')
        stream.write(
            '{"type":"message.user","id":"after","parentId":null,'
            '"timestamp":"2026-07-31 12:00:00","payload":{}}\n'
        )

    with pytest.raises(ValueError, match="Malformed Session Entry"):
        SessionRepository(tmp_path / "sessions").rebuild_session_view(session.id)
