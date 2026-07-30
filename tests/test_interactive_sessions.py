from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from paicli.config import PaiCliConfig
from paicli.entrypoints.repl import start_repl
from paicli.render.textual_widgets import ChatLog, StatusBar
from paicli.render.tui_app import PaiCliApp
from paicli.render.tui_dialogs import SessionResumePicker
from paicli.session import (
    InteractiveSession,
    SessionLeaseConflictError,
    SessionRepository,
    ToolActionSpec,
)
from paicli.types import Message


class HistoryAgent:
    def __init__(self) -> None:
        self.history: list[Message] = []

    def clear_history(self) -> None:
        self.history.clear()

    def replace_history(self, history: list[Message]) -> None:
        self.history = list(history)


class ContextRestoringAgent(HistoryAgent):
    def __init__(self) -> None:
        super().__init__()
        self.context_checkpoint: dict | None = None

    def restore_session_context(
        self,
        history: list[Message],
        context_checkpoint: dict,
    ) -> None:
        self.history = list(history)
        self.context_checkpoint = dict(context_checkpoint)


class CompletingAgent(HistoryAgent):
    async def run(self, message: str):
        assert message == "persist me"
        yield {"type": "text_delta", "text": "durable "}
        yield {"type": "text_delta", "text": "answer"}
        yield {"type": "done", "total_tokens": 3, "total_turns": 1}


class EmptyCompletingAgent(HistoryAgent):
    async def run(self, message: str):
        assert message == "tool-only completion"
        yield {"type": "done", "total_tokens": 1, "total_turns": 1}


class ToolCompletingAgent(HistoryAgent):
    async def run(self, message: str):
        assert message == "inspect with tool"
        call = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"note.txt"}'},
        }
        yield {"type": "text_delta", "text": "I will inspect."}
        yield {
            "type": "turn_complete",
            "turn": 1,
            "stop_reason": "tool_use",
            "message": {
                "role": "assistant",
                "content": "I will inspect.",
                "name": None,
                "tool_call_id": None,
                "tool_calls": [call],
                "reasoning_content": "inspect reasoning",
            },
            "tool_actions": [
                {
                    "tool_call_id": "call_1",
                    "tool_name": "read_file",
                    "arguments": {"path": "note.txt"},
                    "raw_call": call,
                    "is_read_only": True,
                    "is_idempotent": True,
                }
            ],
        }
        yield {
            "type": "tool_call",
            "tool_call_id": "call_1",
            "name": "read_file",
            "input": {"path": "note.txt"},
            "raw_call": call,
            "is_read_only": True,
            "is_idempotent": True,
        }
        yield {
            "type": "tool_result",
            "tool_call_id": "call_1",
            "name": "read_file",
            "result": "1: hello",
            "is_error": False,
        }
        yield {"type": "text_delta", "text": "Inspected."}
        yield {
            "type": "turn_complete",
            "turn": 2,
            "stop_reason": "end_turn",
            "message": {
                "role": "assistant",
                "content": "Inspected.",
                "name": None,
                "tool_call_id": None,
                "tool_calls": [],
                "reasoning_content": None,
            },
        }
        yield {"type": "done", "total_tokens": 3, "total_turns": 2}


class InterruptibleAgent(HistoryAgent):
    async def run(self, message: str):
        assert message == "interrupt me"
        yield {"type": "text_delta", "text": "half answer"}
        await asyncio.Event().wait()
        yield {"type": "done", "total_tokens": 0, "total_turns": 1}


class RecoveringAgent(HistoryAgent):
    def __init__(self) -> None:
        super().__init__()
        self.execution_state: dict | None = None

    async def run(self, message: str, *, execution_state=None):
        assert message == ""
        self.execution_state = execution_state
        assert execution_state["pending_tool_calls"] == []
        assert execution_state["messages"][-1]["role"] == "tool"
        assert "must not be automatically repeated" in execution_state["messages"][-1]["content"]
        yield {"type": "text_delta", "text": "I will inspect state before retrying."}
        yield {
            "type": "turn_complete",
            "turn": 2,
            "stop_reason": "end_turn",
            "message": {
                "role": "assistant",
                "content": "I will inspect state before retrying.",
                "name": None,
                "tool_call_id": None,
                "tool_calls": [],
                "reasoning_content": None,
            },
            "tool_actions": [],
        }
        yield {"type": "done", "total_tokens": 1, "total_turns": 2}


class SafeRecoveringAgent(HistoryAgent):
    def __init__(self) -> None:
        super().__init__()
        self.resumed_calls = 0

    async def run(self, message: str, *, execution_state=None):
        assert message == ""
        call = execution_state["pending_tool_calls"][0]
        assert call["id"] == "call_read"
        self.resumed_calls += 1
        yield {
            "type": "tool_call",
            "tool_call_id": "call_read",
            "name": "read_file",
            "input": {"path": "note.txt"},
            "raw_call": call,
            "is_read_only": True,
            "is_idempotent": True,
        }
        yield {
            "type": "tool_result",
            "tool_call_id": "call_read",
            "name": "read_file",
            "result": "1: hello",
            "is_error": False,
        }
        yield {"type": "text_delta", "text": "Recovered safely."}
        yield {
            "type": "turn_complete",
            "turn": 2,
            "stop_reason": "end_turn",
            "message": {
                "role": "assistant",
                "content": "Recovered safely.",
                "name": None,
                "tool_call_id": None,
                "tool_calls": [],
                "reasoning_content": None,
            },
            "tool_actions": [],
        }
        yield {"type": "done", "total_tokens": 1, "total_turns": 2}


class SettledToolRecoveringAgent(HistoryAgent):
    def __init__(self) -> None:
        super().__init__()
        self.resumed = False

    async def run(self, message: str, *, execution_state=None):
        assert message == ""
        assert execution_state["pending_tool_calls"] == []
        assert execution_state["messages"][-1]["role"] == "tool"
        assert execution_state["messages"][-1]["content"] == "1: hello"
        self.resumed = True
        yield {"type": "text_delta", "text": "Continued after restart."}
        yield {
            "type": "turn_complete",
            "turn": 2,
            "stop_reason": "end_turn",
            "message": {
                "role": "assistant",
                "content": "Continued after restart.",
                "name": None,
                "tool_call_id": None,
                "tool_calls": [],
                "reasoning_content": None,
            },
            "tool_actions": [],
        }
        yield {"type": "done", "total_tokens": 1, "total_turns": 2}


class FailingCompletionRepository(SessionRepository):
    def complete_turn(self, session_id: str, **kwargs):
        del session_id, kwargs
        raise OSError("simulated persistence failure")

    def interrupt_turn(self, session_id: str, *, assistant_content: str, **kwargs):
        if assistant_content:
            raise OSError("simulated persistence failure")
        return super().interrupt_turn(
            session_id,
            assistant_content=assistant_content,
            **kwargs,
        )


def _expire_session_lease(database: Path, session_id: str) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            "update session_leases set expires_at = ? where session_id = ?",
            ("2000-01-01T00:00:00+00:00", session_id),
        )


def test_tui_resumes_latest_workspace_session_and_restores_model_history(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")
    older = repository.create_session(workspace, title="Older")
    repository.append_message(older.id, role="user", content="old question")
    repository.append_message(older.id, role="assistant", content="old answer")
    latest = repository.create_session(workspace, title="Latest")
    repository.append_message(latest.id, role="user", content="latest question")
    repository.append_message(latest.id, role="assistant", content="latest answer")
    agent = HistoryAgent()

    app = PaiCliApp(
        agent=agent,
        cwd=str(workspace),
        session_repository=repository,
    )

    assert app.session_id == latest.id
    assert [(message.role, message.content) for message in agent.history] == [
        ("user", "latest question"),
        ("assistant", "latest answer"),
    ]


def test_tui_restores_compacted_context_checkpoint_and_newer_tail(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(workspace, title="Compacted")
    repository.begin_turn(session.id, turn_id="turn_1", user_content="old question")
    repository.complete_turn(
        session.id,
        turn_id="turn_1",
        assistant_content="old answer",
        context_checkpoint={
            "checkpoint_id": "ctx_123",
            "summary": "The old conversation established the design.",
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "[Previous conversation summary]\n"
                        "The old conversation established the design."
                    ),
                    "name": None,
                    "tool_call_id": None,
                    "tool_calls": [],
                    "reasoning_content": None,
                }
            ],
            "compaction": {
                "compacted_items": 2,
                "protected_items": 0,
                "used_llm": False,
                "llm_usage": {},
            },
            "pressure": {},
            "provider": "openai",
            "model": "gpt-test",
        },
    )
    repository.begin_turn(session.id, turn_id="turn_2", user_content="new question")
    repository.complete_turn(
        session.id,
        turn_id="turn_2",
        assistant_content="new answer",
    )
    agent = ContextRestoringAgent()

    app = PaiCliApp(
        agent=agent,
        cwd=str(workspace),
        session_repository=repository,
        session_id=session.id,
    )

    assert app.session_id == session.id
    assert [(message.role, message.content) for message in agent.history] == [
        (
            "user",
            "[Previous conversation summary]\nThe old conversation established the design.",
        ),
        ("user", "new question"),
        ("assistant", "new answer"),
    ]
    assert agent.context_checkpoint is not None
    assert agent.context_checkpoint["checkpoint_id"] == "ctx_123"


def test_checkpoint_restore_applies_later_message_hidden_events(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(workspace, title="hidden tail")
    repository.begin_turn(session.id, turn_id="turn_hidden", user_content="hide me")
    repository.complete_turn(
        session.id,
        turn_id="turn_hidden",
        assistant_content="keep me",
        context_checkpoint={
            "checkpoint_id": "ctx_hidden",
            "summary": "summary",
            "compaction": {
                "summary": "summary",
                "compacted_items": 2,
                "protected_items": 2,
                "used_llm": False,
                "llm_usage": {},
            },
            "pressure": {},
            "provider": "fake",
            "model": "model",
            "messages": [
                {"role": "user", "content": "hide me"},
                {"role": "assistant", "content": "keep me"},
            ],
        },
    )
    user_message = next(
        message
        for message in repository.rebuild_session_view(session.id).session_history
        if message.role == "user"
    )
    checkpoint_event = next(
        event
        for event in repository.list_events(session.id)
        if event.type == "context.checkpoint_created"
    )
    assert checkpoint_event.payload["messages"][0]["source_message_id"] == user_message.id
    repository.hide_message(session.id, user_message.id)

    interactive = InteractiveSession(repository, workspace, session_id=session.id)

    assert [(message.role, message.content) for message in interactive.agent_history] == [
        ("assistant", "keep me")
    ]
    interactive.close()


def test_interactive_session_skips_busy_session_and_rejects_explicit_conflict(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")

    first = InteractiveSession(repository, workspace)
    second = InteractiveSession(repository, workspace)

    assert second.id != first.id
    with pytest.raises(SessionLeaseConflictError):
        InteractiveSession(repository, workspace, session_id=first.id)

    first.close()
    resumed = InteractiveSession(repository, workspace, session_id=first.id)
    assert resumed.id == first.id


def test_resume_current_session_reacquires_expired_lease(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = tmp_path / "sessions.db"
    repository = SessionRepository(database)
    interactive = InteractiveSession(repository, workspace)
    session_id = interactive.id

    _expire_session_lease(database, session_id)

    interactive.resume_session(session_id)
    interactive.begin_turn("after resume")

    assert interactive.id == session_id


def test_refresh_lease_reacquires_expired_lease(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = tmp_path / "sessions.db"
    repository = SessionRepository(database)
    interactive = InteractiveSession(repository, workspace)

    _expire_session_lease(database, interactive.id)

    interactive.refresh_lease()
    interactive.begin_turn("after heartbeat recovery")


def test_refresh_lease_async_retries_transient_database_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")
    interactive = InteractiveSession(repository, workspace)
    attempts = 0
    original_refresh = repository.refresh_session_lease

    def flaky_refresh(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise sqlite3.OperationalError("database is locked")
        return original_refresh(*args, **kwargs)

    monkeypatch.setattr(repository, "refresh_session_lease", flaky_refresh)

    refreshed = asyncio.run(
        interactive.refresh_lease_async(
            retry_delays=(0, 0),
            lock_timeout_seconds=0.01,
        )
    )

    assert refreshed is True
    assert attempts == 3


def test_resume_current_session_does_not_steal_live_replacement_lease(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = tmp_path / "sessions.db"
    repository = SessionRepository(database)
    interactive = InteractiveSession(repository, workspace)
    session_id = interactive.id

    _expire_session_lease(database, session_id)
    replacement = repository.acquire_session_lease(session_id, owner_id="replacement-owner")

    with pytest.raises(SessionLeaseConflictError):
        interactive.resume_session(session_id)

    repository.refresh_session_lease(session_id, replacement.token)


def test_busy_archived_session_is_not_unarchived_before_lease_acquisition(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")
    archived = repository.create_session(workspace)
    lease = repository.acquire_session_lease(archived.id, owner_id="archiver")
    repository.archive_session(archived.id, lease_token=lease.token)
    interactive = InteractiveSession(repository, workspace)

    with pytest.raises(SessionLeaseConflictError):
        interactive.resume_session(archived.id)

    record = repository.get_session(archived.id)
    assert record is not None and record.archived_at is not None
    interactive.close()
    repository.release_session_lease(archived.id, lease.token)


def test_busy_deleted_session_is_not_restored_before_lease_acquisition(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")
    deleted = repository.create_session(workspace)
    lease = repository.acquire_session_lease(deleted.id, owner_id="deleter")
    repository.delete_session(deleted.id, lease_token=lease.token)
    interactive = InteractiveSession(repository, workspace)

    with pytest.raises(SessionLeaseConflictError):
        interactive.restore_session(deleted.id)

    record = repository.get_session(deleted.id)
    assert record is not None and record.deleted_at is not None
    interactive.close()
    repository.release_session_lease(deleted.id, lease.token)


def test_tui_persists_completed_submission_and_turn_boundary(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")

    async def run() -> None:
        app = PaiCliApp(
            agent=CompletingAgent(),
            cwd=str(workspace),
            session_repository=repository,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            app.run_agent_task("persist me")
            await pilot.pause()
            await app.workers.wait_for_complete()

            view = repository.rebuild_session_view(app.session_id)
            assert [(message.role, message.content) for message in view.session_history] == [
                ("user", "persist me"),
                ("assistant", "durable answer"),
            ]
            assert repository.list_events(app.session_id)[-1].type == "turn.completed"
            assert "durable answer" in app.query_one(ChatLog).renderable_text()

    asyncio.run(run())


def test_tui_persists_empty_assistant_fact_for_successful_turn(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")

    async def run() -> None:
        app = PaiCliApp(
            agent=EmptyCompletingAgent(),
            cwd=str(workspace),
            session_repository=repository,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            app.run_agent_task("tool-only completion")
            await pilot.pause()
            await app.workers.wait_for_complete()

            view = repository.rebuild_session_view(app.session_id)
            assert [(message.role, message.content) for message in view.session_history] == [
                ("user", "tool-only completion"),
                ("assistant", ""),
            ]
            assert repository.list_events(app.session_id)[-1].type == "turn.completed"

    asyncio.run(run())


def test_tui_persists_tool_call_and_result_for_model_replay(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")

    async def run() -> None:
        app = PaiCliApp(
            agent=ToolCompletingAgent(),
            cwd=str(workspace),
            session_repository=repository,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            app.run_agent_task("inspect with tool")
            await pilot.pause()
            await app.workers.wait_for_complete()

    asyncio.run(run())

    restored = HistoryAgent()
    PaiCliApp(
        agent=restored,
        cwd=str(workspace),
        session_repository=repository,
    )

    assert [(message.role, message.content) for message in restored.history] == [
        ("user", "inspect with tool"),
        ("assistant", "I will inspect."),
        ("tool", "1: hello"),
        ("assistant", "Inspected."),
    ]
    assert restored.history[1].tool_calls[0]["id"] == "call_1"
    assert restored.history[1].reasoning_content == "inspect reasoning"
    assert restored.history[2].tool_call_id == "call_1"


def test_tui_persists_one_partial_message_on_interrupt_without_model_replay(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")

    async def run() -> None:
        app = PaiCliApp(
            agent=InterruptibleAgent(),
            cwd=str(workspace),
            session_repository=repository,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            app.run_agent_task("interrupt me")
            await pilot.pause()
            app.action_interrupt()
            await pilot.pause()

            view = repository.rebuild_session_view(app.session_id)
            assert [
                (message.role, message.content, message.status) for message in view.session_history
            ] == [
                ("user", "interrupt me", "complete"),
                ("assistant", "half answer", "partial"),
            ]
            assert [(message.role, message.content) for message in view.model_messages] == [
                ("user", "interrupt me"),
            ]
            assert [event.type for event in repository.list_events(app.session_id)].count(
                "message.assistant.partial"
            ) == 1
            assert repository.list_events(app.session_id)[-1].type == "turn.interrupted"

    asyncio.run(run())


def test_tui_restart_interrupts_orphaned_turn_before_accepting_new_input(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(workspace)
    repository.begin_turn(
        session.id,
        turn_id="turn_orphaned",
        user_content="input before process exit",
    )

    async def run() -> None:
        app = PaiCliApp(
            agent=CompletingAgent(),
            cwd=str(workspace),
            session_repository=repository,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            app.run_agent_task("persist me")
            await pilot.pause()
            await app.workers.wait_for_complete()

            assert "Session error: session already has an active turn" not in (
                app.query_one(ChatLog).renderable_text()
            )

    asyncio.run(run())

    events = repository.list_events(session.id)
    assert [
        event.type
        for event in events
        if event.type in {"turn.started", "turn.interrupted", "turn.completed"}
    ] == [
        "turn.started",
        "turn.interrupted",
        "turn.started",
        "turn.completed",
    ]
    interrupted = next(event for event in events if event.type == "turn.interrupted")
    assert interrupted.payload["reason"] == "process_restarted"


def test_tui_discards_an_incomplete_turn_before_accepting_new_input(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")

    async def run() -> str:
        app = PaiCliApp(
            agent=CompletingAgent(),
            cwd=str(workspace),
            session_repository=repository,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            assert app._interactive_session is not None
            app._interactive_session.begin_turn("orphaned input")

            app.run_agent_task("persist me")
            await pilot.pause()
            await app.workers.wait_for_complete()
            return app.session_id

    session_id = asyncio.run(run())

    boundaries = [
        event
        for event in repository.list_events(session_id)
        if event.type in {"turn.started", "turn.interrupted", "turn.completed"}
    ]
    assert [event.type for event in boundaries] == [
        "turn.started",
        "turn.interrupted",
        "turn.started",
        "turn.completed",
    ]
    assert boundaries[1].payload["reason"] == "superseded_by_new_submission"


def test_tui_discards_an_incomplete_turn_before_switching_sessions(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")

    async def run() -> tuple[str, str]:
        app = PaiCliApp(
            agent=HistoryAgent(),
            cwd=str(workspace),
            session_repository=repository,
        )
        async with app.run_test(size=(80, 24)):
            assert app._interactive_session is not None
            previous_id = app.session_id
            app._interactive_session.begin_turn("orphaned input")

            app._handle_slash_command("/session new")

            return previous_id, app.session_id

    previous_id, current_id = asyncio.run(run())

    assert current_id != previous_id
    boundary = repository.list_events(previous_id)[-1]
    assert boundary.type == "turn.interrupted"
    assert boundary.payload["reason"] == "superseded_by_session_command"


def test_tui_restart_discards_an_uncertain_write_action(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(workspace)
    repository.begin_turn(session.id, turn_id="turn_crashed", user_content="change it")
    repository.prepare_tool_actions(
        session.id,
        turn_id="turn_crashed",
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
    agent = RecoveringAgent()

    async def run() -> None:
        app = PaiCliApp(
            agent=agent,
            cwd=str(workspace),
            session_repository=repository,
        )
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()

    asyncio.run(run())

    assert agent.execution_state is None
    settled = repository.list_pending_actions(session.id, include_settled=True)
    assert settled[0].status == "abandoned"
    terminal = repository.list_events(session.id)[-1]
    assert terminal.type == "turn.interrupted"
    assert terminal.payload["reason"] == "process_restarted"


def test_tui_restart_discards_an_idempotent_read_action(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(workspace)
    repository.begin_turn(session.id, turn_id="turn_crashed", user_content="inspect it")
    repository.prepare_tool_actions(
        session.id,
        turn_id="turn_crashed",
        model_turn=1,
        assistant_content="",
        actions=(
            ToolActionSpec(
                tool_call_id="call_read",
                tool_name="read_file",
                arguments={"path": "note.txt"},
                raw_call={
                    "id": "call_read",
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
    repository.start_tool_action(session.id, "call_read")
    agent = SafeRecoveringAgent()

    async def run() -> None:
        app = PaiCliApp(
            agent=agent,
            cwd=str(workspace),
            session_repository=repository,
        )
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()

    asyncio.run(run())

    assert agent.resumed_calls == 0
    settled = repository.list_pending_actions(session.id, include_settled=True)
    assert settled[0].status == "abandoned"
    history = repository.rebuild_session_view(session.id).model_messages
    assert [message.role for message in history] == ["user", "assistant", "tool"]
    terminal = repository.list_events(session.id)[-1]
    assert terminal.type == "turn.interrupted"
    assert terminal.payload["reason"] == "process_restarted"


def test_tui_restart_closes_a_turn_after_a_durable_tool_result(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(workspace)
    repository.begin_turn(session.id, turn_id="turn_crashed", user_content="inspect it")
    repository.prepare_tool_actions(
        session.id,
        turn_id="turn_crashed",
        model_turn=1,
        assistant_content="",
        actions=(
            ToolActionSpec(
                tool_call_id="call_read",
                tool_name="read_file",
                arguments={"path": "note.txt"},
                raw_call={
                    "id": "call_read",
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
    repository.start_tool_action(session.id, "call_read")
    repository.complete_tool_action(
        session.id,
        "call_read",
        content="1: hello",
        is_error=False,
    )
    agent = SettledToolRecoveringAgent()

    async def run() -> None:
        app = PaiCliApp(
            agent=agent,
            cwd=str(workspace),
            session_repository=repository,
        )
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()

    asyncio.run(run())

    assert agent.resumed is False
    terminal = repository.list_events(session.id)[-1]
    assert terminal.type == "turn.interrupted"
    assert terminal.payload["reason"] == "process_restarted"


def test_restart_preserves_a_durable_denial_without_reasking_or_executing(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(workspace)
    repository.begin_turn(session.id, turn_id="turn_crashed", user_content="write it")
    repository.prepare_tool_actions(
        session.id,
        turn_id="turn_crashed",
        model_turn=1,
        assistant_content="",
        actions=(
            ToolActionSpec(
                tool_call_id="call_write",
                tool_name="write_file",
                arguments={"path": "note.txt"},
                raw_call={"id": "call_write"},
                is_read_only=False,
                is_idempotent=False,
            ),
        ),
    )
    repository.start_tool_action(session.id, "call_write")
    repository.request_tool_approval(session.id, "call_write")
    repository.resolve_tool_approval(session.id, "call_write", decision="deny")

    interactive = InteractiveSession(repository, workspace, session_id=session.id)
    recovery = interactive.prepare_background_task_recovery_state()

    assert recovery is not None
    assert recovery["pending_tool_calls"] == []
    assert recovery["messages"][-1]["role"] == "tool"
    assert "was denied by approval policy" in recovery["messages"][-1]["content"]
    action = repository.list_pending_actions(session.id, include_settled=True)[0]
    assert action.status == "completed"
    assert action.approval_status == "deny"
    interactive.close()


def test_restart_reuses_a_durable_approval_for_a_safe_replay(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(workspace)
    repository.begin_turn(session.id, turn_id="turn_crashed", user_content="inspect it")
    repository.prepare_tool_actions(
        session.id,
        turn_id="turn_crashed",
        model_turn=1,
        assistant_content="",
        actions=(
            ToolActionSpec(
                tool_call_id="call_read",
                tool_name="read_file",
                arguments={"path": "note.txt"},
                raw_call={"id": "call_read"},
                is_read_only=True,
                is_idempotent=True,
            ),
        ),
    )
    repository.start_tool_action(session.id, "call_read")
    repository.request_tool_approval(session.id, "call_read")
    repository.resolve_tool_approval(session.id, "call_read", decision="approve")

    interactive = InteractiveSession(repository, workspace, session_id=session.id)
    recovery = interactive.prepare_background_task_recovery_state()

    assert recovery is not None
    assert recovery["approval_decisions"] == {"call_read": "approve"}
    assert [call["id"] for call in recovery["pending_tool_calls"]] == ["call_read"]
    interactive.close()


def test_approval_request_uses_tool_call_id_for_duplicate_calls(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")
    interactive = InteractiveSession(repository, workspace)
    interactive.begin_turn("write twice")
    raw_calls = (
        {"id": "call_1", "function": {"name": "write_file", "arguments": "{}"}},
        {"id": "call_2", "function": {"name": "write_file", "arguments": "{}"}},
    )
    interactive.record_tool_batch(
        model_turn=1,
        assistant_content="",
        reasoning_content=None,
        actions=[
            {
                "tool_call_id": raw["id"],
                "tool_name": "write_file",
                "arguments": {},
                "raw_call": raw,
                "is_read_only": False,
                "is_idempotent": False,
            }
            for raw in raw_calls
        ],
    )

    matched = interactive.request_tool_approval(
        {"tool_call_id": "call_2", "tool_name": "write_file", "input": {}}
    )

    assert matched == "call_2"
    pending = repository.list_pending_actions(interactive.id)
    assert [action.approval_status for action in pending] == [None, "requested"]
    interactive.close()


def test_tui_persists_inline_tool_approval_decision(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")
    decision: str | None = None

    async def run() -> None:
        nonlocal decision
        app = PaiCliApp(
            agent=HistoryAgent(),
            cwd=str(workspace),
            session_repository=repository,
        )
        async with app.run_test(size=(100, 24)) as pilot:
            call = {
                "id": "call_write",
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": '{"path":"note.txt"}',
                },
            }
            app._interactive_session.begin_turn("write it")
            turn_event = {
                "type": "turn_complete",
                "turn": 1,
                "stop_reason": "tool_use",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [call],
                },
                "tool_actions": [
                    {
                        "tool_call_id": "call_write",
                        "tool_name": "write_file",
                        "arguments": {"path": "note.txt"},
                        "raw_call": call,
                        "is_read_only": False,
                        "is_idempotent": False,
                    }
                ],
            }
            await app._persist_session_event(app._interactive_session, turn_event)
            app.handle_event(turn_event)
            tool_event = {
                "type": "tool_call",
                "tool_call_id": "call_write",
                "name": "write_file",
                "input": {"path": "note.txt"},
            }
            await app._persist_session_event(app._interactive_session, tool_event)
            app.handle_event(tool_event)

            async def request() -> None:
                nonlocal decision
                decision = await app.request_approval(
                    {
                        "tool_name": "write_file",
                        "input": {"path": "note.txt"},
                        "danger_level": "medium",
                        "description": "Write a file",
                    }
                )

            app.run_worker(request())
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()

    asyncio.run(run())

    assert decision == "approve"
    pending = repository.list_pending_actions(repository.list_sessions()[0].id)
    assert pending[0].approval_status == "approve"


def test_tui_reset_persists_context_boundary_across_restart(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(workspace)
    repository.append_message(session.id, role="user", content="before reset")
    first_agent = HistoryAgent()

    async def reset() -> None:
        first_app = PaiCliApp(
            agent=first_agent,
            cwd=str(workspace),
            session_repository=repository,
        )
        async with first_app.run_test(size=(80, 24)) as pilot:
            first_app._handle_slash_command("/reset")
            await pilot.pause()

    asyncio.run(reset())

    second_agent = HistoryAgent()
    second_app = PaiCliApp(
        agent=second_agent,
        cwd=str(workspace),
        session_repository=repository,
    )
    view = repository.rebuild_session_view(second_app.session_id)

    assert [message.content for message in view.session_history] == ["before reset"]
    assert view.model_messages == ()
    assert second_agent.history == []


def test_start_repl_uses_the_user_level_session_database(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}

    class FakeRegistry:
        def list_names(self):
            return []

        def summaries(self):
            return []

    class FakePromptAssembler:
        def __init__(self, **kwargs):
            del kwargs

        def build(self):
            return "system"

    class FakeAgent:
        def __init__(self, **kwargs):
            del kwargs
            self.approval_callback = None

        def close(self):
            return None

    class FakeApp:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run_async(self):
            return None

        async def request_approval(self, *args, **kwargs):
            del args, kwargs
            return "deny"

    async def build_registry(**kwargs):
        del kwargs
        return FakeRegistry(), None

    client = SimpleNamespace(
        model_name="test-model",
        provider_name="test-provider",
        max_context_window=1_000,
    )
    database_path = tmp_path / "home" / ".paicli" / "sessions" / "sessions.db"
    monkeypatch.setattr("paicli.entrypoints.repl.build_tool_registry", build_registry)
    monkeypatch.setattr("paicli.entrypoints.repl.create_llm_client", lambda *args, **kwargs: client)
    monkeypatch.setattr("paicli.entrypoints.repl.PromptAssembler", FakePromptAssembler)
    monkeypatch.setattr("paicli.entrypoints.repl.Agent", FakeAgent)
    monkeypatch.setattr("paicli.entrypoints.repl.PaiCliApp", FakeApp)
    monkeypatch.setattr(
        "paicli.entrypoints.repl.default_session_database_path",
        lambda: database_path,
        raising=False,
    )

    asyncio.run(start_repl(str(tmp_path), PaiCliConfig()))

    assert captured["session_repository"].db_path == database_path


def test_tui_renders_restored_session_history_on_mount(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(workspace)
    repository.append_message(session.id, role="user", content="restored question")
    repository.append_message(
        session.id,
        role="assistant",
        content="restored partial",
        partial=True,
        interruption_reason="user_interrupt",
    )

    async def run() -> None:
        app = PaiCliApp(
            agent=HistoryAgent(),
            cwd=str(workspace),
            session_repository=repository,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            rendered = app.query_one(ChatLog).renderable_text()
            assert "restored question" in rendered
            assert "restored partial" in rendered

    asyncio.run(run())


def test_tui_session_list_shows_only_current_workspace_sessions(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    other_workspace = tmp_path / "other"
    workspace.mkdir()
    other_workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")
    visible = repository.create_session(workspace, title="Visible")
    repository.create_session(other_workspace, title="Hidden")

    async def run() -> None:
        app = PaiCliApp(
            agent=HistoryAgent(),
            cwd=str(workspace),
            session_repository=repository,
        )
        async with app.run_test(size=(100, 24)) as pilot:
            app._handle_slash_command("/session list")
            await pilot.pause()
            rendered = app.query_one(ChatLog).renderable_text()
            assert visible.id in rendered
            assert "Visible" in rendered
            assert "current" in rendered
            assert "Hidden" not in rendered

    asyncio.run(run())


def test_tui_can_create_and_resume_workspace_sessions(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")
    original = repository.create_session(workspace, title="Original")
    repository.append_message(original.id, role="user", content="resume this history")
    agent = HistoryAgent()

    async def run() -> None:
        app = PaiCliApp(
            agent=agent,
            cwd=str(workspace),
            session_repository=repository,
        )
        async with app.run_test(size=(100, 24)) as pilot:
            app._handle_slash_command("/session new Fresh")
            await pilot.pause()
            created_id = app.session_id
            assert created_id != original.id
            assert repository.get_session(created_id).title == "Fresh"
            assert agent.history == []

            app._handle_slash_command(f"/session resume {original.id}")
            await pilot.pause()
            assert app.session_id == original.id
            assert [(message.role, message.content) for message in agent.history] == [
                ("user", "resume this history")
            ]
            assert "resume this history" in app.query_one(ChatLog).renderable_text()

    asyncio.run(run())


def test_tui_session_rename_show_and_resume_picker(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")
    candidate = repository.create_session(workspace, title="Candidate")
    repository.append_message(candidate.id, role="user", content="pick this session")
    current = repository.create_session(workspace, title="Current")

    async def run() -> None:
        app = PaiCliApp(
            agent=HistoryAgent(),
            cwd=str(workspace),
            session_repository=repository,
            session_id=current.id,
        )
        async with app.run_test(size=(100, 30)) as pilot:
            app._handle_slash_command("/session rename Renamed Current")
            app._handle_slash_command(f"/session show {candidate.id}")
            rendered = app.query_one(ChatLog).renderable_text()
            assert "Renamed Current" in rendered
            assert "pick this session" in rendered
            assert "1 messages" in rendered

            app._handle_slash_command("/session resume")
            await pilot.pause()
            assert isinstance(app.screen, SessionResumePicker)
            await pilot.press("enter")
            await pilot.pause()
            assert app.session_id == candidate.id

    asyncio.run(run())


def test_tui_session_share_exports_redacted_markdown(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")
    current = repository.create_session(workspace, title="Shared")
    repository.append_message(
        current.id,
        role="user",
        content=r"token=sk-private-token and D:\private\note.txt",
    )

    async def run() -> None:
        app = PaiCliApp(
            agent=HistoryAgent(),
            cwd=str(workspace),
            session_repository=repository,
            session_id=current.id,
        )
        async with app.run_test(size=(100, 24)) as pilot:
            app._handle_slash_command("/session share")
            await pilot.pause()
            rendered = app.query_one(ChatLog).renderable_text()
            assert "Shared session" in rendered

    asyncio.run(run())
    output = tmp_path / "shares" / f"{current.id}.md"
    markdown = output.read_text(encoding="utf-8")
    assert "[REDACTED_SECRET]" in markdown
    assert "[REDACTED_PATH]" in markdown
    assert "sk-private-token" not in markdown


def test_tui_persists_usage_and_shows_durable_session_stats(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")

    async def run() -> None:
        app = PaiCliApp(
            agent=HistoryAgent(),
            cwd=str(workspace),
            session_repository=repository,
        )
        async with app.run_test(size=(110, 26)):
            session = app._interactive_session
            assert session is not None
            app._provider = "qwen"
            app._model = "qwen-plus"
            session.begin_turn("measure this")
            await app._persist_session_event(
                session,
                {
                    "type": "usage",
                    "request_id": "request-1",
                    "provider": "qwen",
                    "model": "qwen-plus",
                    "usage_source": "actual",
                    "usage": {
                        "input_tokens": 120,
                        "output_tokens": 20,
                        "cached_tokens": 40,
                        "cache_write_tokens": 5,
                    },
                },
            )
            session.complete_turn("measured")
            app._update_status_bar()

            status = app.query_one(StatusBar)
            assert "session ↑80 ↓20 R40 W5" in status.session_text
            assert "CH32%" in status.session_text
            assert "≈¥" in status.session_text

            app._handle_slash_command("/session stats")
            await app.workers.wait_for_complete()
            rendered = app.query_one(ChatLog).renderable_text()
            assert "Session statistics" in rendered
            assert "input 80" in rendered
            assert "cache read 40" in rendered
            assert "coverage complete" in rendered

    asyncio.run(run())


def test_interactive_session_renames_and_exposes_rich_session_summaries(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")
    original = repository.create_session(workspace, title="Original")
    other = repository.create_session(workspace, title="Other")
    repository.append_message(other.id, role="user", content="candidate preview")
    session = InteractiveSession(repository, workspace, session_id=original.id)

    renamed = session.rename_session("Renamed")
    shown = session.show_session(other.id)
    candidates = session.resume_candidates()

    assert renamed.title == "Renamed"
    assert session.record.title == "Renamed"
    assert shown.id == other.id
    assert shown.latest_user_preview == "candidate preview"
    assert [record.id for record in candidates] == [other.id]
    session.close()


def test_show_session_can_inspect_a_corrupt_catalog_record(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")
    current = repository.create_session(workspace, title="Current")
    corrupt = repository.create_session(workspace, title="Corrupt")
    repository._mark_session_corrupt(corrupt.id)
    session = InteractiveSession(repository, workspace, session_id=current.id)

    shown = session.show_session(corrupt.id)

    assert shown.status == "corrupt"
    assert shown.title == "Corrupt"
    session.close()


def test_tui_session_fork_archive_delete_and_restore_lifecycle(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")
    parent = repository.create_session(workspace, title="Parent")
    repository.append_message(parent.id, role="user", content="forked history")
    repository.append_event(parent.id, "turn.completed", {})

    async def run() -> None:
        app = PaiCliApp(
            agent=HistoryAgent(),
            cwd=str(workspace),
            session_repository=repository,
        )
        async with app.run_test(size=(100, 24)) as pilot:
            app._handle_slash_command("/session fork Forked")
            await pilot.pause()
            forked_id = app.session_id
            assert forked_id != parent.id
            assert repository.get_session(forked_id).title == "Forked"
            assert [
                message.content
                for message in repository.rebuild_session_view(forked_id).session_history
            ] == ["forked history"]

            app._handle_slash_command("/session archive")
            await pilot.pause()
            after_archive_id = app.session_id
            assert after_archive_id != forked_id
            assert repository.get_session(forked_id).archived_at is not None

            app._handle_slash_command(f"/session resume {forked_id}")
            await pilot.pause()
            assert app.session_id == forked_id
            assert repository.get_session(forked_id).archived_at is None

            app._handle_slash_command(f"/session resume {after_archive_id}")
            await pilot.pause()
            app._handle_slash_command("/session delete")
            await pilot.pause()
            after_delete_id = app.session_id
            assert after_delete_id != after_archive_id
            assert repository.get_session(after_archive_id).deleted_at is not None

            app._handle_slash_command(f"/session restore {after_archive_id}")
            await pilot.pause()
            assert app.session_id == after_archive_id
            assert repository.get_session(after_archive_id).deleted_at is None

    asyncio.run(run())


def test_tui_fork_uses_context_reset_as_the_latest_safe_boundary(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions.db")
    parent = repository.create_session(workspace)
    repository.append_message(parent.id, role="user", content="old model context")
    repository.append_event(parent.id, "turn.completed", {})
    repository.reset_context(parent.id)
    repository.append_message(parent.id, role="user", content="unfinished after reset")

    async def run() -> None:
        app = PaiCliApp(
            agent=HistoryAgent(),
            cwd=str(workspace),
            session_repository=repository,
        )
        async with app.run_test(size=(100, 24)) as pilot:
            app._handle_slash_command("/session fork Reset fork")
            await pilot.pause()
            view = repository.rebuild_session_view(app.session_id)
            assert view.reset_sequence is not None
            assert view.model_messages == ()
            assert [message.content for message in view.session_history] == ["old model context"]

    asyncio.run(run())


def test_tui_returns_to_idle_when_completion_persistence_fails(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = FailingCompletionRepository(tmp_path / "sessions.db")

    async def run() -> None:
        app = PaiCliApp(
            agent=CompletingAgent(),
            cwd=str(workspace),
            session_repository=repository,
        )
        async with app.run_test(size=(100, 24)) as pilot:
            app.run_agent_task("persist me")
            await pilot.pause()
            await app.workers.wait_for_complete()

            assert app._agent_running is False
            assert app._phase == "idle"
            assert "Session persistence error" in app.query_one(ChatLog).renderable_text()
            assert repository.list_events(app.session_id)[-1].type == "turn.interrupted"

    asyncio.run(run())


def test_tui_interrupt_still_cancels_when_partial_persistence_fails(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = FailingCompletionRepository(tmp_path / "sessions.db")

    async def run() -> None:
        app = PaiCliApp(
            agent=InterruptibleAgent(),
            cwd=str(workspace),
            session_repository=repository,
        )
        async with app.run_test(size=(100, 24)) as pilot:
            app.run_agent_task("interrupt me")
            await pilot.pause()
            app.action_interrupt()
            await pilot.pause()

            assert app._agent_running is False
            assert app._phase == "idle"
            assert "Session persistence error" in app.query_one(ChatLog).renderable_text()
            assert repository.list_events(app.session_id)[-1].type == "turn.interrupted"

    asyncio.run(run())
