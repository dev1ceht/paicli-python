from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from paicli.session import (
    BlobReference,
    SessionCorruptError,
    SessionIdempotencyConflictError,
    SessionReadOnlyError,
    SessionRepository,
)


def test_create_session_is_immediately_available_for_replay_and_listing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")

    created = repository.create_session(workspace, title="Persistent work")

    assert created.workspace_root == str(workspace.resolve())
    assert created.title == "Persistent work"
    assert created.status == "idle"
    assert repository.get_session(created.id) == created
    assert repository.list_sessions() == [created]

    events = repository.list_events(created.id)
    assert [(event.sequence, event.type) for event in events] == [(1, "session.created")]
    assert events[0].payload == {
        "session_id": created.id,
        "title": "Persistent work",
        "workspace_root": str(workspace.resolve()),
    }


def test_append_event_is_ordered_and_idempotent_within_one_session(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)

    first = repository.append_event(
        session.id,
        "message.user",
        {"message_id": "msg_1", "text": "hello"},
        idempotency_key="request-1",
    )
    repeated = repository.append_event(
        session.id,
        "message.user",
        {"message_id": "msg_1", "text": "hello"},
        idempotency_key="request-1",
    )

    assert repeated == first
    assert first.sequence == 2
    assert first.previous_event_hash == repository.list_events(session.id)[0].event_hash

    with pytest.raises(SessionIdempotencyConflictError):
        repository.append_event(
            session.id,
            "message.user",
            {"message_id": "msg_2", "text": "different"},
            idempotency_key="request-1",
        )

    assert [event.sequence for event in repository.list_events(session.id)] == [1, 2]


def test_replay_keeps_transcript_but_excludes_reset_history_and_partial_messages(
    tmp_path: Path,
) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)

    repository.append_message(session.id, role="user", content="old question")
    repository.append_message(session.id, role="assistant", content="old answer")
    reset = repository.reset_context(session.id)
    current = repository.append_message(session.id, role="user", content="current question")
    partial = repository.append_message(
        session.id,
        role="assistant",
        content="unfinished ans",
        partial=True,
        interruption_reason="user_cancelled",
    )

    view = repository.rebuild_session_view(session.id)

    assert [message.content for message in view.transcript] == [
        "old question",
        "old answer",
        "current question",
        "unfinished ans",
    ]
    assert [message.id for message in view.model_messages] == [current.id]
    assert view.reset_sequence == reset.sequence
    assert partial.status == "partial"
    assert partial.replayable is False


def test_corrupt_event_chain_is_rejected_and_session_becomes_read_only(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    repository = SessionRepository(db_path)
    session = repository.create_session(tmp_path)
    repository.append_message(session.id, role="user", content="original")

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            update session_events
            set payload_json = '{"message_id":"msg_tampered","role":"user","parts":[]}'
            where session_id = ? and sequence = 2
            """,
            (session.id,),
        )

    with pytest.raises(SessionCorruptError):
        repository.rebuild_session_view(session.id)

    damaged = repository.get_session(session.id)
    assert damaged is not None
    assert damaged.status == "corrupt"
    with pytest.raises(SessionReadOnlyError):
        repository.append_event(session.id, "message.user", {"text": "must not append"})


def test_blob_content_is_deduplicated_and_referenced_by_events(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)
    content = b"image-bytes-" * 1_000

    stored = repository.put_blob(content, content_type="image/png")
    repeated = repository.put_blob(content, content_type="image/png")
    event = repository.append_event(
        session.id,
        "message.user",
        {"message_id": "msg_image", "role": "user"},
        blob_refs=[BlobReference(content_hash=stored.content_hash, role="image")],
    )

    assert repeated == stored
    assert repository.get_blob(stored.content_hash).data == content
    assert repository.list_event_blob_refs(event.id) == (
        BlobReference(content_hash=stored.content_hash, role="image"),
    )
    assert event.blob_refs == repository.list_event_blob_refs(event.id)


def test_archive_hides_and_freezes_session_until_unarchived(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)

    archived = repository.archive_session(session.id)

    assert archived.archived_at is not None
    assert repository.list_sessions() == []
    assert repository.list_sessions(include_archived=True) == [archived]
    with pytest.raises(SessionReadOnlyError):
        repository.append_message(session.id, role="user", content="blocked")

    restored = repository.unarchive_session(session.id)

    assert restored.archived_at is None
    assert repository.list_sessions() == [restored]
    assert [event.type for event in repository.list_events(session.id)][-2:] == [
        "session.archived",
        "session.unarchived",
    ]


def test_deleted_session_can_be_restored_or_purged_with_orphan_blob_collection(
    tmp_path: Path,
) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)
    blob = repository.put_blob(b"durable attachment", content_type="application/octet-stream")
    repository.append_event(
        session.id,
        "message.user",
        {"message_id": "msg_blob", "role": "user"},
        blob_refs=[BlobReference(content_hash=blob.content_hash, role="attachment")],
    )

    deleted = repository.delete_session(session.id)

    assert deleted.deleted_at is not None
    assert deleted.purge_after is not None
    assert repository.list_sessions() == []
    assert repository.list_sessions(include_deleted=True) == [deleted]
    with pytest.raises(SessionReadOnlyError):
        repository.append_event(session.id, "message.user", {})

    restored = repository.restore_session(session.id)
    assert restored.deleted_at is None
    assert repository.list_sessions() == [restored]

    repository.delete_session(session.id)
    assert repository.purge_session(session.id)
    assert repository.get_session(session.id) is None
    assert repository.get_blob(blob.content_hash) is not None
    assert repository.collect_orphan_blobs() == 1
    assert repository.get_blob(blob.content_hash) is None


def test_fork_copies_semantic_history_and_keeps_blob_alive_after_parent_purge(
    tmp_path: Path,
) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    parent_workspace = tmp_path / "parent"
    child_workspace = tmp_path / "child"
    parent_workspace.mkdir()
    child_workspace.mkdir()
    parent = repository.create_session(parent_workspace, title="Parent")
    blob = repository.put_blob(b"shared image", content_type="image/png")
    source_message = repository.append_event(
        parent.id,
        "message.user",
        {
            "message_id": "msg_1",
            "role": "user",
            "parts": [{"kind": "text", "content": "inspect image", "metadata": {}}],
        },
        blob_refs=[BlobReference(content_hash=blob.content_hash, role="image")],
    )
    repository.append_message(parent.id, role="assistant", content="image inspected")

    child = repository.fork_session(
        parent.id,
        workspace_root=child_workspace,
        title="Child",
    )

    assert child.workspace_root == str(child_workspace.resolve())
    transcript = repository.rebuild_session_view(child.id).transcript
    assert [message.content for message in transcript] == [
        "inspect image",
        "image inspected",
    ]
    copied = next(event for event in repository.list_events(child.id) if event.source_event_id)
    assert copied.source_session_id == parent.id
    assert copied.source_event_id == source_message.id
    assert copied.blob_refs == (BlobReference(content_hash=blob.content_hash, role="image"),)

    assert repository.purge_session(parent.id)
    assert repository.collect_orphan_blobs() == 0
    assert repository.get_blob(blob.content_hash) is not None
    repository.append_message(child.id, role="user", content="continue independently")


def test_hiding_message_changes_projection_without_deleting_original_event(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)
    hidden = repository.append_message(session.id, role="user", content="hide me")
    visible = repository.append_message(session.id, role="user", content="keep me")

    repository.hide_message(session.id, hidden.id)

    view = repository.rebuild_session_view(session.id)
    assert [(message.content, message.hidden) for message in view.transcript] == [
        ("hide me", True),
        ("keep me", False),
    ]
    assert [message.id for message in view.model_messages] == [visible.id]
    assert [event.type for event in repository.list_events(session.id)][-1] == "message.hidden"


def test_expired_recycle_bin_sessions_can_be_purged_in_one_batch(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    expired = repository.create_session(tmp_path, title="expired")
    retained = repository.create_session(tmp_path, title="retained")
    repository.delete_session(expired.id, retention_days=0)
    repository.delete_session(retained.id, retention_days=30)

    purged = repository.purge_expired_sessions()

    assert purged == (expired.id,)
    assert repository.get_session(expired.id) is None
    assert repository.get_session(retained.id) is not None


def test_session_metadata_projection_and_replay_are_updated_together(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(
        tmp_path,
        title="Original",
        metadata={"category": "coding", "remove_me": True},
    )

    updated = repository.update_session_metadata(
        session.id,
        title="Renamed",
        metadata={"category": "review", "remove_me": None},
    )

    assert updated.title == "Renamed"
    assert updated.metadata == {"category": "review"}
    assert repository.rebuild_session_view(session.id).metadata == {
        "category": "review",
        "session_id": session.id,
        "title": "Renamed",
        "workspace_root": str(tmp_path.resolve()),
    }


def test_competing_writers_receive_one_contiguous_session_sequence(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)
    start = Barrier(8)

    def append(index: int) -> None:
        start.wait()
        repository.append_event(session.id, "test.concurrent", {"index": index})

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(append, range(8)))

    events = repository.list_events(session.id)
    assert [event.sequence for event in events] == list(range(1, 10))
    repository.verify_session(session.id)


def test_blob_reference_tampering_marks_session_corrupt(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    repository = SessionRepository(db_path)
    session = repository.create_session(tmp_path)
    blob = repository.put_blob(b"trusted bytes", content_type="application/octet-stream")
    event = repository.append_event(
        session.id,
        "message.user",
        {"message_id": "msg_1", "role": "user"},
        blob_refs=[BlobReference(content_hash=blob.content_hash, role="attachment")],
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "update event_blob_refs set role = 'tampered' where event_id = ?",
            (event.id,),
        )

    with pytest.raises(SessionCorruptError):
        repository.verify_session(session.id)
