from __future__ import annotations

import asyncio
import json
import re
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Barrier

import pytest
from rich.console import Console

from paicli.cancellation import CancellationToken
from paicli.config import load_config
from paicli.entrypoints.repl import _task_command
from paicli.runtime import DurableTaskManager, RuntimeApiServer
from paicli.session import InteractiveSession, SessionRepository
from paicli.tools import ToolRegistry
from paicli.tools.base import ApprovalPending, Tool, ToolResult


def test_durable_task_lifecycle(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = manager.add("do work")

    task = manager.claim_next()
    assert task is not None
    assert task.id == task_id
    assert task.status == "running"
    assert task.started_at is not None
    assert task.finished_at is None
    assert task.duration_seconds is not None

    assert manager.complete(task_id, "done")
    completed = manager.get(task_id)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.result == "done"
    assert completed.finished_at is not None
    assert completed.duration_seconds is not None
    timestamp_pattern = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"
    assert re.fullmatch(timestamp_pattern, completed.created_at)
    assert re.fullmatch(timestamp_pattern, completed.updated_at)
    assert re.fullmatch(timestamp_pattern, completed.started_at)
    assert re.fullmatch(timestamp_pattern, completed.finished_at)


def test_background_task_is_a_child_session_in_the_shared_store(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    parent = repository.create_session(
        tmp_path,
        metadata={"session_kind": "runtime_root"},
    )
    manager = DurableTaskManager(
        tmp_path / "runtime" / "tasks.db",
        session_repository=repository,
        workspace_root=tmp_path,
        parent_session_id=parent.id,
    )

    task_id = manager.add("do work")
    queued = manager.get(task_id)
    assert queued is not None
    assert queued.session_id
    relation = repository.get_parent_relationship(queued.session_id)
    assert relation is not None
    assert relation.parent_session_id == parent.id
    assert relation.relation_type == "background_task"
    assert manager.claim_next() is not None
    assert manager.complete(task_id, "done")
    assert [event.type for event in repository.list_events(queued.session_id)][-3:] == [
        "background_task.queued",
        "background_task.running",
        "background_task.completed",
    ]
    assert manager.db_path.name == "tasks.db"
    assert manager.db_path.parent.name == "runtime"


@pytest.mark.skip(reason="Task SQLite and Session JSONL do not share a cross-store transaction")
def test_background_task_creation_rolls_back_with_its_queued_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    parent = repository.create_session(tmp_path)
    manager = DurableTaskManager(
        repository,
        workspace_root=tmp_path,
        parent_session_id=parent.id,
    )
    original_append = repository._append_event_in_transaction

    def fail_queued_event(connection, **kwargs):
        if kwargs["event_type"] == "background_task.queued":
            raise RuntimeError("injected event failure")
        return original_append(connection, **kwargs)

    monkeypatch.setattr(repository, "_append_event_in_transaction", fail_queued_event)

    with pytest.raises(RuntimeError, match="injected event failure"):
        manager.add("do work")

    assert manager.list() == []
    assert repository.list_child_sessions(parent.id) == []


@pytest.mark.skip(reason="Task SQLite and Session JSONL do not share a cross-store transaction")
def test_background_task_claim_rolls_back_with_its_running_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = DurableTaskManager(tmp_path / "sessions.db")
    task_id = manager.add("do work")
    original_append = manager.repository._append_event_in_transaction

    def fail_running_event(connection, **kwargs):
        if kwargs["event_type"] == "background_task.running":
            raise RuntimeError("injected event failure")
        return original_append(connection, **kwargs)

    monkeypatch.setattr(
        manager.repository,
        "_append_event_in_transaction",
        fail_running_event,
    )

    with pytest.raises(RuntimeError, match="injected event failure"):
        manager.claim_next()

    assert manager.get(task_id).status == "queued"  # type: ignore[union-attr]


def test_interactive_session_can_queue_its_own_background_task(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    interactive = InteractiveSession(repository, tmp_path)
    manager = DurableTaskManager(
        tmp_path / "tasks.db",
        session_repository=repository,
        workspace_root=tmp_path,
        parent_session_id=interactive.id,
    )

    task_id = manager.add("inspect later")

    task = manager.get(task_id)
    assert task is not None
    relation = repository.get_parent_relationship(task.session_id)
    assert relation is not None
    assert relation.parent_session_id == interactive.id
    runtime_root = repository.get_or_create_root_session(
        tmp_path,
        root_kind="runtime_root",
        title="Runtime",
    )
    runtime_manager = DurableTaskManager(
        tmp_path / "tasks.db",
        session_repository=repository,
        workspace_root=tmp_path,
        parent_session_id=runtime_root.id,
    )
    assert runtime_manager.claim_next().id == task_id  # type: ignore[union-attr]
    interactive.close()


def test_durable_task_cancel(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = manager.add("do work")

    assert manager.cancel(task_id)
    canceled = manager.get(task_id)
    assert canceled is not None
    assert canceled.status == "canceled"
    assert canceled.started_at is None
    assert canceled.finished_at is not None
    assert canceled.duration_seconds is None


def test_cancel_closes_the_task_turn_in_the_same_transaction(tmp_path: Path) -> None:
    manager = DurableTaskManager(tmp_path / "sessions.db")
    task_id = manager.add("do work")
    task = manager.claim_next()
    assert task is not None
    interactive = InteractiveSession(
        manager.repository,
        tmp_path,
        session_id=task.session_id,
    )
    interactive.begin_turn("do work")

    assert manager.cancel(task_id)

    assert [event.type for event in manager.repository.list_events(task.session_id)][-2:] == [
        "turn.interrupted",
        "background_task.canceled",
    ]
    interactive.close()


def test_task_record_exposes_duration_in_api_payload(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = manager.add("do work")
    assert manager.claim_next() is not None
    assert manager.complete(task_id, "done")

    task = manager.get(task_id)
    assert task is not None
    payload = task.to_dict()
    assert payload["started_at"] == task.started_at
    assert payload["finished_at"] == task.finished_at
    assert payload["duration_seconds"] == task.duration_seconds


def test_only_one_worker_can_claim_a_queued_task(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = manager.add("do work")
    start = Barrier(4)

    def claim():
        start.wait()
        return manager.claim_next()

    with ThreadPoolExecutor(max_workers=4) as executor:
        claims = list(executor.map(lambda _: claim(), range(4)))

    claimed = [task for task in claims if task is not None]
    assert [task.id for task in claimed] == [task_id]


def test_task_workers_only_claim_their_own_session_queue(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    first_root = repository.create_session(tmp_path / "first")
    second_root = repository.create_session(tmp_path / "second")
    first = DurableTaskManager(
        tmp_path / "tasks.db",
        session_repository=repository,
        workspace_root=tmp_path / "first",
        parent_session_id=first_root.id,
    )
    second = DurableTaskManager(
        tmp_path / "tasks.db",
        session_repository=repository,
        workspace_root=tmp_path / "second",
        parent_session_id=second_root.id,
    )
    first_id = first.add("first")
    second_id = second.add("second")

    assert first.claim_next().id == first_id  # type: ignore[union-attr]
    assert second.claim_next().id == second_id  # type: ignore[union-attr]


@pytest.mark.skip(reason="Exclusive task claims were removed for the single-process scheduler")
def test_live_task_claim_is_not_failed_by_another_manager(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    root = repository.create_session(tmp_path)
    first = DurableTaskManager(repository, parent_session_id=root.id)
    second = DurableTaskManager(repository, parent_session_id=root.id)
    task_id = first.add("work")
    assert first.claim_next() is not None

    assert second.fail_interrupted_tasks() == 0
    assert second.get(task_id).status == "running"  # type: ignore[union-attr]


@pytest.mark.skip(reason="Task claim TTLs were removed for the single-process scheduler")
def test_expired_task_claim_cannot_be_refreshed_or_complete(tmp_path: Path) -> None:
    manager = DurableTaskManager(
        tmp_path / "sessions.db",
        claim_ttl_seconds=0,
    )
    task_id = manager.add("work")
    assert manager.claim_next() is not None

    with pytest.raises(RuntimeError, match="claim is no longer owned"):
        manager.refresh_claim(task_id)
    assert not manager.complete(task_id, "stale result")
    assert manager.fail_interrupted_tasks() == 1
    assert manager.get(task_id).status == "failed"  # type: ignore[union-attr]


def test_task_management_is_scoped_to_its_root_session(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    first_root = repository.create_session(tmp_path / "first")
    second_root = repository.create_session(tmp_path / "second")
    first = DurableTaskManager(
        tmp_path / "tasks.db",
        session_repository=repository,
        workspace_root=tmp_path / "first",
        parent_session_id=first_root.id,
    )
    second = DurableTaskManager(
        tmp_path / "tasks.db",
        session_repository=repository,
        workspace_root=tmp_path / "second",
        parent_session_id=second_root.id,
    )
    task_id = first.add("private")

    assert second.get(task_id) is None
    assert second.list() == []
    assert not second.cancel(task_id)
    assert first.get(task_id) is not None


def test_terminal_statuses_cannot_be_overwritten(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = manager.add("do work")
    assert manager.claim_next() is not None

    assert manager.cancel(task_id)
    assert not manager.complete(task_id, "late result")
    assert not manager.fail(task_id, "late error")

    task = manager.get(task_id)
    assert task is not None
    assert task.status == "canceled"
    assert task.result is None
    assert task.error is None


def test_completed_task_is_terminal(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = manager.add("do work")
    assert manager.claim_next() is not None

    assert manager.complete(task_id, "done")
    assert not manager.cancel(task_id)
    assert not manager.fail(task_id, "late error")


def test_waiting_approval_can_be_approved_or_canceled_atomically(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = manager.add("change a file")
    assert manager.claim_next() is not None

    approval = manager.wait_for_approval(
        task_id,
        checkpoint={"next_tool_index": 0, "messages": []},
        request={"tool_name": "write_file", "input": {"path": "notes.txt"}},
    )
    assert approval is not None
    assert manager.get(task_id).status == "waiting_approval"  # type: ignore[union-attr]

    assert manager.approve(task_id)
    assert manager.get(task_id).status == "queued"  # type: ignore[union-attr]
    assert not manager.approve(task_id)

    assert manager.claim_next() is not None
    assert manager.wait_for_approval(task_id, checkpoint={}, request={}) is not None
    assert manager.cancel(task_id)
    assert manager.get(task_id).status == "canceled"  # type: ignore[union-attr]
    assert not manager.approve(task_id)


@pytest.mark.skip(reason="Task SQLite and Session JSONL do not share a cross-store transaction")
def test_agent_approval_pause_rolls_back_as_one_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = DurableTaskManager(tmp_path / "sessions.db")
    task_id = manager.add("change a file")
    task = manager.claim_next()
    assert task is not None
    interactive = InteractiveSession(
        manager.repository,
        tmp_path,
        session_id=task.session_id,
    )
    turn_id = interactive.begin_turn("change a file")
    interactive.record_tool_batch(
        model_turn=1,
        assistant_content="",
        reasoning_content=None,
        actions=[
            {
                "tool_call_id": "call_write",
                "tool_name": "write",
                "arguments": {"path": "note.txt"},
                "raw_call": {"id": "call_write"},
                "is_read_only": False,
                "is_idempotent": False,
            }
        ],
    )
    original_append = manager.repository._append_event_in_transaction

    def fail_approval_event(connection, **kwargs):
        if kwargs["event_type"] == "approval.requested":
            raise RuntimeError("injected approval failure")
        return original_append(connection, **kwargs)

    monkeypatch.setattr(
        manager.repository,
        "_append_event_in_transaction",
        fail_approval_event,
    )

    with pytest.raises(RuntimeError, match="injected approval failure"):
        manager.wait_for_approval(
            task_id,
            checkpoint={"active_tool_call_id": "call_write"},
            request={"tool_name": "write", "input": {"path": "note.txt"}},
            session_id=interactive.id,
            tool_call_id="call_write",
            lease_token=interactive.lease_token,
        )

    assert manager.get(task_id).status == "running"  # type: ignore[union-attr]
    action = manager.repository.list_pending_actions(interactive.id)[0]
    assert action.status == "prepared"
    assert action.turn_id == turn_id
    interactive.interrupt_turn("", reason="test_cleanup")
    interactive.close()


def test_denied_approval_is_recorded_in_the_execution_checkpoint(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = manager.add("change a file")
    assert manager.claim_next() is not None
    assert (
        manager.wait_for_approval(
            task_id,
            checkpoint={"next_tool_index": 1},
            request={"tool_name": "bash", "input": {"command": "echo $TOKEN"}},
        )
        is not None
    )

    assert manager.deny(task_id, source="api")
    assert manager.get(task_id).status == "queued"  # type: ignore[union-attr]
    assert manager.get_checkpoint(task_id) == {
        "next_tool_index": 1,
        "approval_decision": "denied",
    }
    approval = manager.list_approvals(task_id)[0]
    assert approval.status == "denied"
    assert approval.decision_source == "api"


def test_runtime_api_approves_a_waiting_task(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "home"))
    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-key",
    )
    server.task_manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = server.task_manager.add("change a file")
    assert server.task_manager.claim_next() is not None
    assert server.task_manager.wait_for_approval(task_id, checkpoint={}, request={}) is not None

    request = _ApiRequest("POST", f"/v1/tasks/{task_id}/approve", "test-key")
    server._handle(request)

    assert request.status == 200
    assert json.loads(request.wfile.getvalue()) == {"approved": True, "status": "queued"}
    assert server.task_manager.get(task_id).status == "queued"  # type: ignore[union-attr]


def test_runtime_threads_use_the_session_store_without_runtime_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    repository = SessionRepository(home / ".paicli" / "sessions" / "sessions.db")
    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-key",
        session_repository=repository,
    )

    thread_id = server._create_thread()

    thread = repository.get_session(thread_id)
    assert thread is not None
    assert thread.metadata["session_kind"] == "runtime_thread"
    assert repository.get_parent_relationship(thread_id).relation_type == "runtime_thread"  # type: ignore[union-attr]
    assert not (home / ".paicli" / "runtime" / "runtime.db").exists()
    request = _ApiRequest("GET", f"/v1/threads/{thread_id}/events", "test-key")
    server._handle(request)
    assert request.status == 200
    assert b"session.created" in request.wfile.getvalue()


def test_runtime_thread_events_reject_other_session_kinds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "home"))
    repository = SessionRepository(tmp_path / "sessions.db")
    other = repository.create_session(tmp_path)
    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-key",
        session_repository=repository,
    )

    request = _ApiRequest("GET", f"/v1/threads/{other.id}/events", "test-key")
    server._handle(request)

    assert request.status == 404


def test_runtime_task_api_returns_its_child_session_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "home"))
    repository = SessionRepository(tmp_path / "sessions.db")
    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-key",
        session_repository=repository,
    )
    request = _ApiRequest(
        "POST",
        "/v1/tasks",
        "test-key",
        body={"prompt": "inspect later"},
    )

    server._handle(request)

    payload = json.loads(request.wfile.getvalue())
    assert request.status == 200
    assert payload["session_id"]
    assert payload["parent_session_id"] == server.runtime_root.id
    assert repository.get_parent_relationship(payload["session_id"]) is not None


def test_runtime_turns_restore_session_history_between_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PAICLI_SNAPSHOT_DIR", str(tmp_path / "snapshots"))

    class HistoryClient:
        model_name = "fake-model"
        provider_name = "fake-provider"
        max_context_window = 128_000

        def __init__(self) -> None:
            self.requests: list[list[tuple[str, str]]] = []

        async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
            self.requests.append([(message.role, str(message.content)) for message in messages])
            yield {"type": "text_delta", "text": f"answer-{len(self.requests)}"}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    client = HistoryClient()
    registry = ToolRegistry()

    async def build_registry(**kwargs):  # noqa: ARG001
        return registry, None

    monkeypatch.setattr("paicli.runtime.api.build_tool_registry", build_registry)
    monkeypatch.setattr(
        "paicli.runtime.api.create_llm_client",
        lambda _config, **_kwargs: client,
    )
    repository = SessionRepository(tmp_path / "sessions.db")
    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-key",
        session_repository=repository,
    )
    server.config.llm.api_key = "test-key"
    thread_id = server._create_thread()

    first = asyncio.run(server._run_turn(thread_id, "first"))
    second = asyncio.run(server._run_turn(thread_id, "second"))

    assert first["text"] == "answer-1"
    assert second["text"] == "answer-2"
    assert client.requests[1] == [
        ("user", "first"),
        ("assistant", "answer-1"),
        ("user", "second"),
    ]
    assert [
        (message.role, message.content)
        for message in repository.rebuild_session_view(thread_id).model_messages
    ] == [
        ("user", "first"),
        ("assistant", "answer-1"),
        ("user", "second"),
        ("assistant", "answer-2"),
    ]


def test_runtime_api_denies_a_waiting_task(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "home"))
    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-key",
    )
    server.task_manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = server.task_manager.add("change a file")
    assert server.task_manager.claim_next() is not None
    assert server.task_manager.wait_for_approval(task_id, checkpoint={}, request={}) is not None

    request = _ApiRequest("POST", f"/v1/tasks/{task_id}/deny", "test-key")
    server._handle(request)

    assert request.status == 200
    assert json.loads(request.wfile.getvalue()) == {"denied": True, "status": "queued"}
    assert server.task_manager.list_approvals(task_id)[0].status == "denied"


def test_runtime_api_task_detail_redacts_approval_input(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "home"))
    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-key",
    )
    server.task_manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = server.task_manager.add("change a file")
    assert server.task_manager.claim_next() is not None
    assert (
        server.task_manager.wait_for_approval(
            task_id,
            checkpoint={},
            request={"tool_name": "bash", "input": {"token": "secret-value"}},
        )
        is not None
    )

    request = _ApiRequest("GET", f"/v1/tasks/{task_id}", "test-key")
    server._handle(request)

    payload = json.loads(request.wfile.getvalue())
    assert payload["status"] == "waiting_approval"
    assert payload["approvals"][0]["request"]["input"]["token"] == "***"


def test_task_cli_approves_and_shows_a_waiting_approval(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    manager = DurableTaskManager(
        Path.home() / ".paicli" / "runtime" / "tasks.db",
        session_repository=SessionRepository(Path.home() / ".paicli" / "sessions"),
        workspace_root=tmp_path,
    )
    task_id = manager.add("change a file")
    assert manager.claim_next() is not None
    assert (
        manager.wait_for_approval(
            task_id,
            checkpoint={},
            request={"tool_name": "write_file", "input": {"path": "notes.txt"}},
        )
        is not None
    )
    console = Console(record=True)

    _task_command("", console)
    _task_command("approve 1", console)

    output = console.export_text()
    assert "waiting_approval" in output
    assert "Approved: True" in output
    assert manager.get(task_id).status == "queued"  # type: ignore[union-attr]


def test_background_task_resumes_the_approved_tool_from_its_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "home"))
    monkeypatch.setenv("PAICLI_SNAPSHOT_DIR", str(tmp_path / "snapshots"))

    class ApprovalClient:
        model_name = "fake-model"
        provider_name = "fake-provider"
        max_context_window = 128_000

        def __init__(self):
            self.calls = 0

        async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
            self.calls += 1
            if self.calls == 1:
                yield {
                    "type": "tool_call_delta",
                    "tool_call": {
                        "index": 0,
                        "id": "call_write",
                        "function": {"name": "write", "arguments": '{"path":"note.txt"}'},
                    },
                }
                yield {"type": "message_end", "stop_reason": "tool_use"}
                return
            assert any(message.role == "tool" for message in messages)
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    client = ApprovalClient()
    calls: list[dict] = []
    registry = ToolRegistry()

    async def write_handler(payload, context):  # noqa: ARG001
        calls.append(payload)
        return ToolResult("written")

    registry.register(
        Tool(
            name="write",
            description="write a file",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            handler=write_handler,
            is_read_only=False,
            requires_approval=True,
        )
    )
    monkeypatch.setattr(
        "paicli.runtime.api.create_llm_client",
        lambda _config, **_kwargs: client,
    )

    async def build_registry(**kwargs):  # noqa: ARG001
        return registry, None

    monkeypatch.setattr("paicli.runtime.api.build_tool_registry", build_registry)
    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-key",
        session_repository=SessionRepository(tmp_path / "sessions"),
    )
    server.config.llm.api_key = "test-key"
    server.config.policy.hitl_mode = "auto"
    server.config.policy.audit_log_path = str(tmp_path / "audit")
    server.task_manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = server.task_manager.add("write a note")
    assert server.task_manager.claim_next() is not None

    with pytest.raises(ApprovalPending):
        asyncio.run(server._run_task(task_id, "write a note"))

    assert calls == []
    assert server.task_manager.get(task_id).status == "waiting_approval"  # type: ignore[union-attr]
    checkpoint = server.task_manager.get_checkpoint(task_id)
    assert checkpoint["next_tool_index"] == 0  # type: ignore[index]
    assert "tool_call_id" not in checkpoint["approval_request"]  # type: ignore[index]
    assert server.task_manager.approve(task_id)
    assert server.task_manager.claim_next() is not None

    assert asyncio.run(server._run_task(task_id, "write a note")) == "done"
    assert calls == [{"path": "note.txt"}]
    assert client.calls == 2
    task = server.task_manager.get(task_id)
    assert task is not None
    history = server.task_manager.repository.rebuild_session_view(task.session_id).model_messages
    assert [message.role for message in history] == ["user", "assistant", "tool", "assistant"]
    assert (
        server.task_manager.repository.list_pending_actions(
            task.session_id,
            include_settled=True,
        )[0].status
        == "completed"
    )


def test_changed_runtime_identity_requires_a_fresh_approval(tmp_path, monkeypatch):
    registry = ToolRegistry()

    async def build_registry(**kwargs):  # noqa: ARG001
        return registry, None

    monkeypatch.setattr("paicli.runtime.api.build_tool_registry", build_registry)
    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-key",
        session_repository=SessionRepository(tmp_path / "sessions"),
    )
    server.config.llm.api_key = "test-key"
    server.task_manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = server.task_manager.add("write a note")
    assert server.task_manager.claim_next() is not None
    assert (
        server.task_manager.wait_for_approval(
            task_id,
            checkpoint={
                "messages": [],
                "pending_tool_calls": [],
                "runtime_identity": {"cwd": "different"},
            },
            request={"tool_name": "write", "input": {"path": "note.txt"}},
        )
        is not None
    )
    assert server.task_manager.approve(task_id)
    assert server.task_manager.claim_next() is not None

    with pytest.raises(ApprovalPending):
        asyncio.run(server._run_task(task_id, "write a note"))

    assert server.task_manager.get(task_id).status == "waiting_approval"  # type: ignore[union-attr]
    assert [approval.status for approval in server.task_manager.list_approvals(task_id)] == [
        "approved",
        "invalidated",
        "requested",
    ]


class _ApiRequest:
    def __init__(
        self,
        method: str,
        path: str,
        api_key: str,
        body: dict | None = None,
    ):
        self.command = method
        self.path = path
        encoded = json.dumps(body).encode() if body is not None else b""
        self.headers = {
            "x-api-key": api_key,
            "content-length": str(len(encoded)),
        }
        self.rfile = BytesIO(encoded)
        self.wfile = BytesIO()
        self.status: int | None = None

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, _name: str, _value: str) -> None:
        return

    def end_headers(self) -> None:
        return


def test_runtime_startup_marks_interrupted_tasks_failed(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = manager.add("do work")
    task = manager.claim_next()
    assert task is not None
    interactive = InteractiveSession(
        manager.repository,
        tmp_path,
        session_id=task.session_id,
    )
    interactive.begin_turn("do work")
    interactive.close()

    assert manager.fail_interrupted_tasks() == 1
    task = manager.get(task_id)
    assert task is not None
    assert task.status == "failed"
    assert task.finished_at is not None
    assert task.error == (
        "Task interrupted by a previous Runtime shutdown; not retried automatically."
    )
    task_session = manager.get(task_id)
    assert task_session is not None
    event_types = [event.type for event in manager.repository.list_events(task_session.session_id)]
    assert event_types[-2:] == ["turn.interrupted", "background_task.failed"]


@pytest.mark.skip(reason="Session lease heartbeat was removed for single-process JSONL Sessions")
def test_runtime_execution_refreshes_its_session_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    interactive = InteractiveSession(repository, tmp_path)
    interactive.begin_turn("wait")
    refreshed = asyncio.Event()
    original_refresh = interactive.refresh_lease_async

    async def refresh_lease() -> bool:
        result = await original_refresh()
        refreshed.set()
        return result

    monkeypatch.setattr(interactive, "refresh_lease_async", refresh_lease)
    monkeypatch.setattr("paicli.runtime.api.SESSION_LEASE_REFRESH_SECONDS", 0.01)

    class WaitingEngine:
        async def ask(self, *args, **kwargs):  # noqa: ARG002
            await asyncio.wait_for(refreshed.wait(), timeout=1)
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "done"}

    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-key",
        session_repository=repository,
    )

    assert asyncio.run(server._execute_session_turn(interactive, WaitingEngine(), "wait")) == "done"
    interactive.close()


@pytest.mark.skip(reason="Session lease heartbeat was removed for single-process JSONL Sessions")
def test_lost_session_heartbeat_stops_agent_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    interactive = InteractiveSession(repository, tmp_path)
    interactive.begin_turn("wait")
    started = asyncio.Event()

    async def lose_lease() -> bool:
        raise RuntimeError("lease lost")

    monkeypatch.setattr(interactive, "refresh_lease_async", lose_lease)
    monkeypatch.setattr("paicli.runtime.api.SESSION_LEASE_REFRESH_SECONDS", 0.01)

    class WaitingEngine:
        async def ask(self, *args, **kwargs):  # noqa: ARG002
            started.set()
            await asyncio.Event().wait()
            yield {"type": "done"}  # pragma: no cover

    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-key",
        session_repository=repository,
    )

    with pytest.raises(RuntimeError, match="lease lost"):
        asyncio.run(server._execute_session_turn(interactive, WaitingEngine(), "wait"))

    assert started.is_set()
    assert [event.type for event in repository.list_events(interactive.id)][-1] == (
        "turn.interrupted"
    )
    interactive.close()


def test_canceled_runtime_execution_interrupts_its_active_turn(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    interactive = InteractiveSession(repository, tmp_path)
    interactive.begin_turn("wait")
    started = asyncio.Event()

    class WaitingEngine:
        async def ask(self, *args, **kwargs):  # noqa: ARG002
            started.set()
            await asyncio.Event().wait()
            yield {"type": "done"}  # pragma: no cover

    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-key",
        session_repository=repository,
    )

    async def cancel_execution() -> None:
        operation = asyncio.create_task(
            server._execute_session_turn(interactive, WaitingEngine(), "wait")
        )
        await started.wait()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation

    asyncio.run(cancel_execution())

    assert [event.type for event in repository.list_events(interactive.id)][-1] == (
        "turn.interrupted"
    )
    interactive.close()


def test_retry_creates_a_queued_task_linked_to_a_failed_task(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    failed_id = manager.add("do work")
    assert manager.claim_next() is not None
    assert manager.fail(failed_id, "connection lost")

    retry_id = manager.retry(failed_id)
    assert retry_id is not None
    retry = manager.get(retry_id)
    assert retry is not None
    assert retry.status == "queued"
    assert retry.prompt == "do work"
    assert retry.retry_of == failed_id
    assert manager.get(failed_id).status == "failed"  # type: ignore[union-attr]


def test_retry_rejects_non_failed_tasks(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    queued_id = manager.add("do work")
    assert manager.retry(queued_id) is None
    assert manager.cancel(queued_id)
    assert manager.retry(queued_id) is None


def test_task_references_resolve_list_numbers_latest_and_full_ids(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    older_id = manager.add("older")
    newer_id = manager.add("newer")

    assert manager.resolve_reference("1").id == newer_id  # type: ignore[union-attr]
    assert manager.resolve_reference("2").id == older_id  # type: ignore[union-attr]
    assert manager.resolve_reference("latest").id == newer_id  # type: ignore[union-attr]
    assert manager.resolve_reference(older_id).id == older_id  # type: ignore[union-attr]
    assert manager.resolve_reference("3") is None


def test_runtime_cancel_signals_the_active_task(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "home"))
    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-key",
    )
    server.task_manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = server.task_manager.add("do work")
    assert server.task_manager.claim_next() is not None
    signal = CancellationToken()
    with server._task_cancellations_lock:
        server._task_cancellations[task_id] = signal

    assert server._cancel_task(task_id)
    assert signal.is_set()
    task = server.task_manager.get(task_id)
    assert task is not None
    assert task.status == "canceled"
