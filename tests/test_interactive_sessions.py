from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from paicli.config import PaiCliConfig
from paicli.entrypoints.repl import start_repl
from paicli.render.textual_widgets import ChatLog
from paicli.render.tui_app import PaiCliApp
from paicli.session import SessionRepository
from paicli.types import Message


class HistoryAgent:
    def __init__(self) -> None:
        self.history: list[Message] = []

    def clear_history(self) -> None:
        self.history.clear()

    def replace_history(self, history: list[Message]) -> None:
        self.history = list(history)


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


class InterruptibleAgent(HistoryAgent):
    async def run(self, message: str):
        assert message == "interrupt me"
        yield {"type": "text_delta", "text": "half answer"}
        await asyncio.Event().wait()
        yield {"type": "done", "total_tokens": 0, "total_turns": 1}


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
