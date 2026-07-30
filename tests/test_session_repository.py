from __future__ import annotations

import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from paicli.session import (
    BlobReference,
    SessionCorruptError,
    SessionIdempotencyConflictError,
    SessionLeaseConflictError,
    SessionReadOnlyError,
    SessionRepository,
    ToolActionSpec,
)
from paicli.session.integrity import EventHashMaterial, canonical_json
from paicli.session.schema import (
    _migration_1,
    _migration_2,
    _migration_3,
    _migration_4,
    _migration_5,
    connect,
)
from paicli.session.versions import DATABASE_SCHEMA_VERSION


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


def test_child_session_relationship_is_durable_and_queryable(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    parent = repository.create_session(tmp_path, title="Runtime root")

    child = repository.create_child_session(
        parent.id,
        relation_type="background_task",
        title="Inspect project",
        metadata={"task_id": "task_1"},
    )

    relation = repository.get_parent_relationship(child.id)
    assert relation is not None
    assert relation.parent_session_id == parent.id
    assert relation.child_session_id == child.id
    assert relation.relation_type == "background_task"
    assert relation.metadata == {"task_id": "task_1"}
    assert repository.list_child_sessions(parent.id) == [child]
    assert repository.list_events(child.id)[-1].type == "session.parent_linked"
    assert repository.list_events(parent.id)[-1].type == "session.child_linked"


def test_root_session_creation_is_atomic_under_concurrency(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    start = Barrier(4)

    def resolve_root() -> str:
        start.wait()
        return repository.get_or_create_root_session(
            tmp_path,
            root_kind="runtime_root",
            title="Runtime",
        ).id

    with ThreadPoolExecutor(max_workers=4) as executor:
        root_ids = list(executor.map(lambda _: resolve_root(), range(4)))

    assert len(set(root_ids)) == 1


def test_v1_database_is_backed_up_and_migrated_to_current_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _migration_1(connection)
        connection.execute("insert into schema_migrations(version, applied_at) values (1, 'test')")
        connection.execute("pragma user_version = 1")
        connection.commit()

    SessionRepository(db_path)

    with sqlite3.connect(db_path) as connection:
        version = connection.execute("pragma user_version").fetchone()[0]
        columns = {
            row[1] for row in connection.execute("pragma table_info(event_blob_refs)").fetchall()
        }
        migrations = [
            row[0]
            for row in connection.execute(
                "select version from schema_migrations order by version"
            ).fetchall()
        ]
    assert version == DATABASE_SCHEMA_VERSION == 6
    assert "ordinal" in columns
    assert migrations == [1, 2, 3, 4, 5, 6]
    assert len(list(tmp_path.glob("sessions.backup-v1-*.db"))) == 1


def test_v4_catalog_data_is_backfilled_during_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for version, migration in enumerate(
            (_migration_1, _migration_2, _migration_3, _migration_4),
            start=1,
        ):
            migration(connection)
            connection.execute(
                "insert into schema_migrations(version, applied_at) values (?, 'test')",
                (version,),
            )
        connection.execute(
            """
            insert into sessions values (
                'sess_old', ?, 'Old', 'idle', 'created', 'updated',
                null, null, null, 4, 3, '{}'
            )
            """,
            (str(tmp_path.resolve()),),
        )
        payloads = (
            (
                "evt_user",
                1,
                "message.user",
                {
                    "message_id": "msg_user",
                    "role": "user",
                    "parts": [{"kind": "text", "content": "legacy preview", "metadata": {}}],
                },
                "created",
            ),
            (
                "evt_compact",
                2,
                "context.compacted",
                {
                    "checkpoint_id": "ctx_old",
                    "summary": "summary",
                    "compaction": {},
                    "provider": "legacy-provider",
                    "model": "legacy-model",
                },
                "compacted",
            ),
            (
                "evt_checkpoint",
                3,
                "context.checkpoint_created",
                {"checkpoint_id": "ctx_old", "messages": []},
                "checkpoint",
            ),
        )
        for event_id, sequence, event_type, payload, created_at in payloads:
            connection.execute(
                """
                insert into session_events values (
                    ?, 'sess_old', ?, null, ?, 1, ?, null, null, ?,
                    ?, null, null, '[]'
                )
                """,
                (
                    event_id,
                    sequence,
                    event_type,
                    json.dumps(payload),
                    f"hash-{sequence}",
                    created_at,
                ),
            )
        connection.execute("pragma user_version = 4")
        connection.commit()

    record = SessionRepository(db_path).get_session("sess_old")

    assert record is not None
    assert record.message_count == 1
    assert record.user_turn_count == 1
    assert record.latest_user_preview == "legacy preview"
    assert record.provider == "legacy-provider"
    assert record.model == "legacy-model"
    assert record.last_checkpoint_id == "ctx_old"
    assert record.last_compacted_at == "compacted"


def test_v5_utc_timestamps_are_read_as_east_eight_without_rehashing_events(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "sessions.db"
    legacy_timestamp = "2026-01-02T03:04:05+00:00"
    payload = {
        "session_id": "sess_legacy",
        "title": "Legacy",
        "workspace_root": str(tmp_path.resolve()),
    }
    payload_json = canonical_json(payload)
    event_hash = EventHashMaterial(
        event_id="evt_legacy",
        session_id="sess_legacy",
        sequence=1,
        event_type="session.created",
        payload_json=payload_json,
        schema_version=1,
        created_at=legacy_timestamp,
        previous_event_hash=None,
        turn_id=None,
        idempotency_key=None,
        source_session_id=None,
        source_event_id=None,
        blob_refs_json="[]",
    ).digest()
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for version, migration in enumerate(
            (_migration_1, _migration_2, _migration_3, _migration_4, _migration_5),
            start=1,
        ):
            migration(connection)
            connection.execute(
                "insert into schema_migrations(version, applied_at) values (?, ?)",
                (version, legacy_timestamp),
            )
        connection.execute(
            """
            insert into sessions(
                id, workspace_root, title, status, created_at, updated_at,
                next_sequence, version, metadata_json
            ) values (
                'sess_legacy', ?, 'Legacy', 'idle', ?, ?, 2, 1, '{}'
            )
            """,
            (str(tmp_path.resolve()), legacy_timestamp, legacy_timestamp),
        )
        connection.execute(
            """
            insert into session_events(
                id, session_id, sequence, type, schema_version, payload_json,
                previous_event_hash, event_hash, created_at, blob_refs_json
            ) values (
                'evt_legacy', 'sess_legacy', 1, 'session.created', 1, ?,
                null, ?, ?, '[]'
            )
            """,
            (payload_json, event_hash, legacy_timestamp),
        )
        connection.execute("pragma user_version = 5")
        connection.commit()

    repository = SessionRepository(db_path)
    record = repository.get_session("sess_legacy")
    event = repository.list_events("sess_legacy")[0]

    assert record is not None
    assert record.created_at == "2026-01-02 11:04:05"
    assert event.created_at == "2026-01-02 11:04:05"
    repository.verify_session("sess_legacy")
    with sqlite3.connect(db_path) as connection:
        raw_event_timestamp = connection.execute(
            "select created_at from session_events where id = 'evt_legacy'"
        ).fetchone()[0]
    assert raw_event_timestamp == legacy_timestamp


def test_append_event_is_ordered_and_idempotent_within_one_session(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)

    first = repository.append_event(
        session.id,
        "test.message",
        {"message_id": "msg_1", "text": "hello"},
        idempotency_key="request-1",
    )
    repeated = repository.append_event(
        session.id,
        "test.message",
        {"message_id": "msg_1", "text": "hello"},
        idempotency_key="request-1",
    )

    assert repeated == first
    assert first.sequence == 2
    assert first.previous_event_hash == repository.list_events(session.id)[0].event_hash

    with pytest.raises(SessionIdempotencyConflictError):
        repository.append_event(
            session.id,
            "test.message",
            {"message_id": "msg_2", "text": "different"},
            idempotency_key="request-1",
        )

    assert [event.sequence for event in repository.list_events(session.id)] == [1, 2]


def test_append_message_reuses_original_message_for_same_idempotency_key(
    tmp_path: Path,
) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)

    first = repository.append_message(
        session.id,
        role="user",
        content="same request",
        idempotency_key="message-request-1",
    )
    repeated = repository.append_message(
        session.id,
        role="user",
        content="same request",
        idempotency_key="message-request-1",
    )

    assert repeated == first
    assert len(repository.rebuild_session_view(session.id).session_history) == 1


def test_replay_keeps_session_history_but_excludes_reset_and_partial_messages(
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

    assert [message.content for message in view.session_history] == [
        "old question",
        "old answer",
        "current question",
        "unfinished ans",
    ]
    assert [message.id for message in view.model_messages] == [current.id]
    assert view.reset_sequence == reset.sequence
    assert partial.status == "partial"
    assert partial.replayable is False


def test_completed_turn_persists_latest_context_compaction_checkpoint(
    tmp_path: Path,
) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)
    repository.begin_turn(
        session.id,
        turn_id="turn_1",
        user_content="continue the refactor",
    )

    repository.complete_turn(
        session.id,
        turn_id="turn_1",
        assistant_content="implemented",
        context_checkpoint={
            "checkpoint_id": "ctx_123",
            "summary": "Earlier work established the repository boundary.",
            "messages": [
                {
                    "role": "user",
                    "content": "continue the refactor",
                    "name": None,
                    "tool_call_id": None,
                    "tool_calls": [],
                    "reasoning_content": None,
                },
                {
                    "role": "assistant",
                    "content": "implemented",
                    "name": None,
                    "tool_call_id": None,
                    "tool_calls": [],
                    "reasoning_content": None,
                },
            ],
            "compaction": {
                "compacted_items": 8,
                "protected_items": 2,
                "used_llm": True,
                "llm_usage": {"input_tokens": 120, "output_tokens": 30},
            },
            "pressure": {
                "tier": "tier1_snip",
                "pressure_ratio": 0.42,
                "rendered_tokens": 420,
                "raw_tokens": 420,
                "budget_tokens": 1000,
            },
            "provider": "openai",
            "model": "gpt-test",
        },
    )

    events = repository.list_events(session.id)
    assert [event.type for event in events][-4:] == [
        "message.assistant",
        "turn.completed",
        "context.compacted",
        "context.checkpoint_created",
    ]
    view = repository.rebuild_session_view(session.id)
    assert view.context_checkpoint is not None
    assert view.context_checkpoint["checkpoint_id"] == "ctx_123"
    assert view.context_checkpoint["summary"] == (
        "Earlier work established the repository boundary."
    )
    assert view.context_checkpoint["messages"][-1]["content"] == "implemented"
    assert view.context_checkpoint_sequence == events[-1].sequence


def test_manual_compaction_checkpoint_is_persisted_without_creating_a_turn(tmp_path):
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)
    repository.begin_turn(session.id, turn_id="turn_1", user_content="question")
    repository.complete_turn(
        session.id,
        turn_id="turn_1",
        assistant_content="answer",
    )
    before = repository.list_events(session.id)

    repository.save_context_checkpoint(
        session.id,
        {
            "checkpoint_id": "ctx_manual",
            "summary": "Earlier work was summarized manually.",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "[Previous conversation summary]\n"
                        "Earlier work was summarized manually."
                    ),
                    "name": None,
                    "tool_call_id": None,
                    "tool_calls": [],
                    "reasoning_content": None,
                },
                {
                    "role": "user",
                    "content": "question",
                    "name": None,
                    "tool_call_id": None,
                    "tool_calls": [],
                    "reasoning_content": None,
                },
                {
                    "role": "assistant",
                    "content": "answer",
                    "name": None,
                    "tool_call_id": None,
                    "tool_calls": [],
                    "reasoning_content": None,
                },
            ],
            "compaction": {
                "summary": "Earlier work was summarized manually.",
                "compacted_items": 4,
                "protected_items": 2,
                "used_llm": False,
                "llm_usage": {"input_tokens": 20, "output_tokens": 5},
                "llm_usage_records": [
                    {
                        "input_tokens": 12,
                        "output_tokens": 3,
                        "usage_source": "estimated",
                    },
                    {
                        "input_tokens": 8,
                        "output_tokens": 2,
                        "usage_source": "actual",
                    },
                ],
            },
            "pressure": {},
            "provider": "fake",
            "model": "summary-model",
        },
    )

    events = repository.list_events(session.id)
    manual_events = events[len(before) :]
    assert [event.type for event in manual_events] == [
        "context.compacted",
        "usage.recorded",
        "usage.recorded",
        "context.checkpoint_created",
    ]
    assert not any(event.type.startswith("turn.") for event in manual_events)
    assert len({event.turn_id for event in manual_events}) == 1
    usages = [event.payload for event in manual_events if event.type == "usage.recorded"]
    assert [usage["purpose"] for usage in usages] == [
        "context_summary",
        "context_summary",
    ]
    assert [usage["usage_source"] for usage in usages] == ["estimated", "actual"]
    assert [usage["tokens"] for usage in usages] == [
        {"input": 12, "output": 3, "cache_read": 0, "cache_write": 0},
        {"input": 8, "output": 2, "cache_read": 0, "cache_write": 0},
    ]
    view = repository.rebuild_session_view(session.id)
    assert view.context_checkpoint is not None
    assert view.context_checkpoint["checkpoint_id"] == "ctx_manual"
    assert view.context_checkpoint["messages"][-1]["source_message_id"] == (
        view.model_messages[-1].id
    )


def test_rejected_manual_summary_persists_usage_without_compaction_event(tmp_path):
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)

    repository.save_context_summary_usage(
        session.id,
        provider="fake",
        model="summary-model",
        records=[
            {
                "input_tokens": 9,
                "output_tokens": 2,
                "cache_read_tokens": 1,
                "usage_source": "actual",
            }
        ],
    )

    events = repository.list_events(session.id)
    assert [event.type for event in events[-1:]] == ["usage.recorded"]
    assert not any(event.type == "context.compacted" for event in events)
    assert events[-1].payload["purpose"] == "context_summary"
    assert events[-1].payload["tokens"] == {
        "input": 9,
        "output": 2,
        "cache_read": 1,
        "cache_write": 0,
    }


def test_unchanged_context_checkpoint_is_not_duplicated(tmp_path):
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path, title="checkpoint")
    checkpoint = {
        "checkpoint_id": "ctx_same",
        "summary": "same summary",
        "compaction": {
            "summary": "same summary",
            "compacted_items": 2,
            "protected_items": 2,
            "used_llm": False,
            "llm_usage": {},
        },
        "pressure": {},
        "provider": "fake",
        "model": "model",
        "messages": [{"role": "assistant", "content": "summary"}],
    }
    repository.begin_turn(session.id, turn_id="turn_1", user_content="one")
    repository.complete_turn(
        session.id,
        turn_id="turn_1",
        assistant_content="first",
        context_checkpoint=checkpoint,
    )
    repository.begin_turn(session.id, turn_id="turn_2", user_content="two")
    repository.complete_turn(
        session.id,
        turn_id="turn_2",
        assistant_content="second",
        context_checkpoint=checkpoint,
    )

    context_events = [
        event
        for event in repository.list_events(session.id)
        if event.type.startswith("context.")
    ]
    assert [event.type for event in context_events] == [
        "context.compacted",
        "context.checkpoint_created",
    ]


def test_session_timestamps_use_east_eight_without_timezone_or_fraction(tmp_path: Path) -> None:
    before = (datetime.now(UTC) + timedelta(hours=8)).replace(tzinfo=None)
    repository = SessionRepository(tmp_path / "sessions.db")

    created = repository.create_session(tmp_path, title="East eight")
    after = (datetime.now(UTC) + timedelta(hours=8)).replace(tzinfo=None)

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", created.created_at)
    parsed = datetime.strptime(created.created_at, "%Y-%m-%d %H:%M:%S")
    assert before - timedelta(seconds=2) <= parsed <= after + timedelta(seconds=2)
    assert created.updated_at == created.created_at
    assert repository.list_events(created.id)[0].created_at == created.created_at
    lease = repository.acquire_session_lease(created.id, owner_id="time-test")
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
        lease.expires_at,
    )
    blob = repository.put_blob(b"time", content_type="text/plain")
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
        blob.created_at,
    )


def test_session_catalog_uses_event_order_when_updates_share_one_second(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "paicli.session.repository._now",
        lambda: "2026-07-29 16:30:00",
    )
    repository = SessionRepository(tmp_path / "sessions.db")
    first = repository.create_session(tmp_path, title="first")
    second = repository.create_session(tmp_path, title="second")
    recent = min((first, second), key=lambda item: item.id)
    stale = max((first, second), key=lambda item: item.id)

    repository.update_session_metadata(recent.id, title="updated last")

    sessions = repository.list_sessions()
    assert [session.id for session in sessions[:2]] == [recent.id, stale.id]


def test_large_context_checkpoint_messages_use_blob_storage(tmp_path):
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path, title="large checkpoint")
    large_content = "checkpoint content " * 5000
    repository.begin_turn(session.id, turn_id="turn_large", user_content="compact")
    repository.complete_turn(
        session.id,
        turn_id="turn_large",
        assistant_content="done",
        context_checkpoint={
            "checkpoint_id": "ctx_large",
            "summary": "large",
            "compaction": {
                "summary": "large",
                "compacted_items": 5,
                "protected_items": 2,
                "used_llm": False,
                "llm_usage": {},
            },
            "pressure": {},
            "provider": "fake",
            "model": "large",
            "messages": [{"role": "assistant", "content": large_content}],
        },
    )

    event = next(
        event
        for event in repository.list_events(session.id)
        if event.type == "context.checkpoint_created"
    )
    assert event.payload["messages"] == []
    assert event.payload["messages_content_hash"]
    assert [reference.role for reference in event.blob_refs] == ["context.checkpoint.messages"]
    checkpoint = repository.rebuild_session_view(session.id).context_checkpoint
    assert checkpoint is not None
    assert checkpoint["messages"][0]["content"] == large_content


def test_session_catalog_projects_rich_summary_from_events(tmp_path):
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path, title="catalog")
    repository.begin_turn(
        session.id,
        turn_id="turn_catalog",
        user_content="  explain   the catalog projection  ",
    )
    repository.complete_turn(
        session.id,
        turn_id="turn_catalog",
        assistant_content="It keeps list queries fast.",
        context_checkpoint={
            "checkpoint_id": "ctx_catalog",
            "summary": "catalog summary",
            "compaction": {
                "summary": "catalog summary",
                "compacted_items": 4,
                "protected_items": 2,
                "used_llm": False,
                "llm_usage": {},
            },
            "pressure": {},
            "provider": "openai",
            "model": "gpt-catalog",
            "messages": [{"role": "assistant", "content": "catalog summary"}],
        },
    )

    record = repository.get_session(session.id)

    assert record is not None
    assert record.message_count == 2
    assert record.user_turn_count == 1
    assert record.latest_user_preview == "explain the catalog projection"
    assert record.latest_assistant_preview == "It keeps list queries fast."
    assert record.provider == "openai"
    assert record.model == "gpt-catalog"
    assert record.last_checkpoint_id == "ctx_catalog"
    assert record.last_compacted_at is not None
    assert repository.list_sessions()[0] == record


def test_session_catalog_previews_blob_backed_large_messages(tmp_path):
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path, title="large preview")
    repository.append_message(
        session.id,
        role="user",
        content="large preview marker " + ("x" * 70_000),
    )

    record = repository.get_session(session.id)

    assert record is not None
    assert record.latest_user_preview is not None
    assert record.latest_user_preview.startswith("large preview marker")
    assert len(record.latest_user_preview) == 160


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
        "attachment.added",
        {"message_id": "msg_image", "role": "user"},
        blob_refs=[BlobReference(content_hash=stored.content_hash, role="image")],
    )

    assert repeated == stored
    assert repository.get_blob(stored.content_hash).data == content
    assert repository.list_event_blob_refs(event.id) == (
        BlobReference(content_hash=stored.content_hash, role="image"),
    )
    assert event.blob_refs == repository.list_event_blob_refs(event.id)


def test_blob_reference_order_and_duplicates_are_part_of_the_event(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)
    first = repository.put_blob(b"first", content_type="application/octet-stream")
    second = repository.put_blob(b"second", content_type="application/octet-stream")
    references = (
        BlobReference(content_hash=first.content_hash, role="attachment"),
        BlobReference(content_hash=second.content_hash, role="attachment"),
        BlobReference(content_hash=first.content_hash, role="attachment"),
    )

    event = repository.append_event(
        session.id,
        "test.blob_order",
        {},
        blob_refs=references,
    )

    assert event.blob_refs == references
    assert repository.list_event_blob_refs(event.id) == references


def test_large_message_text_is_offloaded_but_replayed_transparently(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)
    content = "x" * (64 * 1024 + 1)

    message = repository.append_message(session.id, role="user", content=content)

    event = repository.list_events(session.id)[-1]
    assert event.payload["parts"][0]["content"] == ""
    assert event.payload["parts"][0]["metadata"]["content_hash"] == event.blob_refs[0].content_hash
    assert message.content == content
    assert repository.rebuild_session_view(session.id).session_history[-1].content == content


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
        "attachment.added",
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
    repository.append_event(parent.id, "turn.completed", {})

    child = repository.fork_session(
        parent.id,
        workspace_root=child_workspace,
        title="Child",
    )

    assert child.workspace_root == str(child_workspace.resolve())
    session_history = repository.rebuild_session_view(child.id).session_history
    assert [message.content for message in session_history] == [
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


def test_fork_requires_turn_boundary_and_uses_metadata_at_that_boundary(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    parent = repository.create_session(tmp_path, metadata={"phase": "before"})
    repository.append_message(parent.id, role="user", content="bounded")
    boundary = repository.append_event(parent.id, "turn.completed", {})
    repository.update_session_metadata(parent.id, metadata={"phase": "after"})

    child = repository.fork_session(parent.id, through_sequence=boundary.sequence)
    assert child.metadata == {"phase": "before"}
    assert repository.rebuild_session_view(child.id).metadata["phase"] == "before"

    repository.append_event(parent.id, "turn.started", {})
    with pytest.raises(ValueError, match="turn boundary"):
        repository.fork_session(parent.id)


def test_fork_title_override_wins_over_copied_title_history(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    parent = repository.create_session(tmp_path, title="Original")
    repository.update_session_metadata(parent.id, title="Renamed")
    repository.append_event(parent.id, "turn.completed", {})

    child = repository.fork_session(parent.id, title="Explicit fork title")
    view = repository.rebuild_session_view(child.id)

    assert child.title == "Explicit fork title"
    assert view.metadata["title"] == "Explicit fork title"


def test_hiding_message_changes_projection_without_deleting_original_event(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)
    hidden = repository.append_message(session.id, role="user", content="hide me")
    visible = repository.append_message(session.id, role="user", content="keep me")

    repository.hide_message(session.id, hidden.id)

    view = repository.rebuild_session_view(session.id)
    assert [(message.content, message.hidden) for message in view.session_history] == [
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


def test_internal_metadata_writes_use_the_same_payload_validation(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path, title="Valid")

    with pytest.raises(TypeError, match="title"):
        repository.update_session_metadata(session.id, title="")

    assert repository.get_session(session.id).title == "Valid"
    repository.verify_session(session.id)


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


def test_session_lease_excludes_competing_owners_until_release(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)

    lease = repository.acquire_session_lease(session.id, owner_id="first-owner")

    with pytest.raises(SessionLeaseConflictError):
        repository.acquire_session_lease(session.id, owner_id="second-owner")

    repository.refresh_session_lease(session.id, lease.token)
    assert repository.release_session_lease(session.id, lease.token)

    replacement = repository.acquire_session_lease(session.id, owner_id="second-owner")
    assert replacement.owner_id == "second-owner"


def test_live_session_lease_rejects_writes_without_its_token(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)
    lease = repository.acquire_session_lease(session.id, owner_id="owner")

    with pytest.raises(SessionLeaseConflictError):
        repository.append_message(session.id, role="user", content="unowned write")

    written = repository.append_message(
        session.id,
        role="user",
        content="owned write",
        lease_token=lease.token,
    )
    assert written.content == "owned write"


def test_expired_session_lease_fences_its_previous_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)
    lease = repository.acquire_session_lease(session.id, owner_id="owner")
    monkeypatch.setattr(
        "paicli.session.repository._now",
        lambda: "9999-12-31 23:59:59",
    )

    with pytest.raises(SessionLeaseConflictError):
        repository.append_message(
            session.id,
            role="user",
            content="stale owner write",
            lease_token=lease.token,
        )


def test_prepared_tool_action_is_durable_before_execution(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)
    repository.begin_turn(session.id, turn_id="turn_1", user_content="inspect it")
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"path":"note.txt"}'},
    }

    prepared = repository.prepare_tool_actions(
        session.id,
        turn_id="turn_1",
        model_turn=1,
        assistant_content="I will inspect it.",
        actions=(
            ToolActionSpec(
                tool_call_id="call_1",
                tool_name="read_file",
                arguments={"path": "note.txt"},
                raw_call=call,
                is_read_only=True,
                is_idempotent=True,
            ),
        ),
    )

    assert prepared[0].status == "prepared"
    assert prepared[0].raw_call == call
    assert repository.list_pending_actions(session.id) == list(prepared)
    assistant = repository.rebuild_session_view(session.id).model_messages[-1]
    assert assistant.role == "assistant"
    assert assistant.content == "I will inspect it."
    assert assistant.parts[-1].kind == "tool_call"
    assert assistant.parts[-1].metadata["tool_call_id"] == "call_1"


def test_pending_tool_actions_preserve_the_model_batch_order(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)
    repository.begin_turn(session.id, turn_id="turn_1", user_content="do both")

    repository.prepare_tool_actions(
        session.id,
        turn_id="turn_1",
        model_turn=1,
        assistant_content="",
        actions=tuple(
            ToolActionSpec(
                tool_call_id=call_id,
                tool_name=f"tool_{call_id}",
                arguments={},
                raw_call={"id": call_id},
                is_read_only=False,
                is_idempotent=False,
            )
            for call_id in ("call_z", "call_a")
        ),
    )

    pending = repository.list_pending_actions(session.id)
    assert [action.tool_call_id for action in pending] == ["call_z", "call_a"]
    assert [action.batch_index for action in pending] == [0, 1]


def test_tool_result_atomically_settles_its_pending_action(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)
    repository.begin_turn(session.id, turn_id="turn_1", user_content="inspect it")
    repository.prepare_tool_actions(
        session.id,
        turn_id="turn_1",
        model_turn=1,
        assistant_content="",
        actions=(
            ToolActionSpec(
                tool_call_id="call_1",
                tool_name="read_file",
                arguments={"path": "note.txt"},
                raw_call={
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"note.txt"}',
                    },
                },
                is_read_only=True,
                is_idempotent=True,
            ),
        ),
    )

    executing = repository.start_tool_action(session.id, "call_1")
    result = repository.complete_tool_action(
        session.id,
        "call_1",
        content="1: hello",
        is_error=False,
    )

    assert executing.status == "executing"
    assert result.role == "tool"
    assert result.content == "1: hello"
    assert result.parts[0].metadata["tool_call_id"] == "call_1"
    assert repository.list_pending_actions(session.id) == []
    settled = repository.list_pending_actions(session.id, include_settled=True)
    assert settled[0].status == "completed"
    assert repository.rebuild_session_view(session.id).model_messages[-1] == result


def test_uncertain_tool_action_is_closed_with_a_paired_unknown_result(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)
    repository.begin_turn(session.id, turn_id="turn_1", user_content="change it")
    repository.prepare_tool_actions(
        session.id,
        turn_id="turn_1",
        model_turn=1,
        assistant_content="",
        actions=(
            ToolActionSpec(
                tool_call_id="call_write",
                tool_name="write_file",
                arguments={"path": "note.txt"},
                raw_call={
                    "id": "call_write",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": '{"path":"note.txt"}',
                    },
                },
                is_read_only=False,
                is_idempotent=False,
            ),
        ),
    )
    repository.start_tool_action(session.id, "call_write")

    result = repository.abandon_tool_action(
        session.id,
        "call_write",
        reason="process_restarted",
    )

    assert result.role == "tool"
    assert result.parts[0].metadata["execution_outcome"] == "unknown"
    assert "must not be automatically repeated" in result.content
    settled = repository.list_pending_actions(session.id, include_settled=True)
    assert settled[0].status == "abandoned"


def test_pending_tool_approval_is_durable_and_resolvable(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)
    repository.begin_turn(session.id, turn_id="turn_1", user_content="write it")
    repository.prepare_tool_actions(
        session.id,
        turn_id="turn_1",
        model_turn=1,
        assistant_content="",
        actions=(
            ToolActionSpec(
                tool_call_id="call_write",
                tool_name="write_file",
                arguments={"path": "note.txt"},
                raw_call={
                    "id": "call_write",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": '{"path":"note.txt"}',
                    },
                },
                is_read_only=False,
                is_idempotent=False,
            ),
        ),
    )
    repository.start_tool_action(session.id, "call_write")

    waiting = repository.request_tool_approval(session.id, "call_write")
    approved = repository.resolve_tool_approval(
        session.id,
        "call_write",
        decision="approve",
    )

    assert waiting.status == "waiting_approval"
    assert waiting.approval_status == "requested"
    assert approved.status == "executing"
    assert approved.approval_status == "approve"
    assert [event.type for event in repository.list_events(session.id)][-2:] == [
        "approval.requested",
        "approval.resolved",
    ]


def test_settled_tool_action_cannot_be_resurrected_by_approval(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)
    repository.begin_turn(session.id, turn_id="turn_1", user_content="inspect it")
    repository.prepare_tool_actions(
        session.id,
        turn_id="turn_1",
        model_turn=1,
        assistant_content="",
        actions=(
            ToolActionSpec(
                tool_call_id="call_1",
                tool_name="read_file",
                arguments={},
                raw_call={"id": "call_1"},
                is_read_only=True,
                is_idempotent=True,
            ),
        ),
    )
    repository.complete_tool_action(
        session.id,
        "call_1",
        content="done",
        is_error=False,
    )

    with pytest.raises(ValueError, match="already settled"):
        repository.request_tool_approval(session.id, "call_1")

    action = repository.list_pending_actions(session.id, include_settled=True)[0]
    assert action.status == "completed"
    assert action.approval_status is None


def test_interrupting_turn_closes_all_unsettled_tool_actions(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)
    repository.begin_turn(session.id, turn_id="turn_1", user_content="change it")
    repository.prepare_tool_actions(
        session.id,
        turn_id="turn_1",
        model_turn=1,
        assistant_content="",
        actions=(
            ToolActionSpec(
                tool_call_id="call_write",
                tool_name="write_file",
                arguments={"path": "note.txt"},
                raw_call={
                    "id": "call_write",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": '{"path":"note.txt"}',
                    },
                },
                is_read_only=False,
                is_idempotent=False,
            ),
        ),
    )
    repository.start_tool_action(session.id, "call_write")

    repository.interrupt_turn(
        session.id,
        turn_id="turn_1",
        assistant_content="",
        reason="user_interrupt",
    )

    assert repository.list_pending_actions(session.id) == []
    roles = [message.role for message in repository.rebuild_session_view(session.id).model_messages]
    assert roles == ["user", "assistant", "tool"]
    assert repository.list_events(session.id)[-1].type == "turn.interrupted"


def test_blob_reference_tampering_marks_session_corrupt(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    repository = SessionRepository(db_path)
    session = repository.create_session(tmp_path)
    blob = repository.put_blob(b"trusted bytes", content_type="application/octet-stream")
    event = repository.append_event(
        session.id,
        "attachment.added",
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


def test_malformed_blob_reference_payload_marks_session_corrupt(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    repository = SessionRepository(db_path)
    session = repository.create_session(tmp_path)
    event = repository.append_event(session.id, "test.event", {})

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "update session_events set blob_refs_json = '{' where id = ?",
            (event.id,),
        )

    with pytest.raises(SessionCorruptError):
        repository.verify_session(session.id)
    assert repository.get_session(session.id).status == "corrupt"


def test_invalid_message_payload_marks_session_corrupt_during_rebuild(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    repository = SessionRepository(db_path)
    session = repository.create_session(tmp_path)
    event = repository.append_message(session.id, role="user", content="valid first")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "update session_events set payload_json = ? where id = ?",
            (
                '{"message_id":"msg_invalid","parts":"not-an-array","role":"user"}',
                event.event_id,
            ),
        )

    with pytest.raises(SessionCorruptError):
        repository.rebuild_session_view(session.id)
    assert repository.get_session(session.id).status == "corrupt"


def test_public_append_enforces_partial_and_hidden_message_invariants(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)

    with pytest.raises(TypeError, match="message_id"):
        repository.append_event(session.id, "message.hidden", {})
    with pytest.raises(ValueError, match="partial and non-replayable"):
        repository.append_event(
            session.id,
            "message.assistant.partial",
            {
                "message_id": "msg_invalid_partial",
                "role": "assistant",
                "parts": [{"kind": "text", "content": "half", "metadata": {}}],
                "status": "complete",
                "replayable": True,
            },
        )
    with pytest.raises(TypeError, match="tool_call_id"):
        repository.append_event(
            session.id,
            "message.assistant",
            {
                "message_id": "msg_invalid_tool_call",
                "role": "assistant",
                "parts": [
                    {
                        "kind": "tool_call",
                        "content": "",
                        "metadata": {"tool_name": "read_file", "raw_call": {}},
                    }
                ],
                "status": "complete",
                "replayable": True,
            },
        )


def test_public_append_cannot_bypass_lifecycle_projection(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path)

    with pytest.raises(ValueError, match="reserved"):
        repository.append_event(session.id, "session.archived", {})

    assert repository.get_session(session.id).archived_at is None
