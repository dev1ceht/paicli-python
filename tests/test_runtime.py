from __future__ import annotations

import asyncio
import json
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
    manager = DurableTaskManager(Path.home() / ".paicli" / "tasks" / "tasks.db")
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
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

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
    assert server.task_manager.get_checkpoint(task_id)["next_tool_index"] == 0  # type: ignore[index]
    assert server.task_manager.approve(task_id)
    assert server.task_manager.claim_next() is not None

    assert asyncio.run(server._run_task(task_id, "write a note")) == "done"
    assert calls == [{"path": "note.txt"}]
    assert client.calls == 2


def test_changed_runtime_identity_requires_a_fresh_approval(tmp_path, monkeypatch):
    registry = ToolRegistry()

    async def build_registry(**kwargs):  # noqa: ARG001
        return registry, None

    monkeypatch.setattr("paicli.runtime.api.build_tool_registry", build_registry)
    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-key",
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
    def __init__(self, method: str, path: str, api_key: str):
        self.command = method
        self.path = path
        self.headers = {"x-api-key": api_key, "content-length": "0"}
        self.rfile = BytesIO()
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
    assert manager.claim_next() is not None

    assert manager.fail_interrupted_tasks() == 1
    task = manager.get(task_id)
    assert task is not None
    assert task.status == "failed"
    assert task.finished_at is not None
    assert task.error == (
        "Task interrupted by a previous Runtime shutdown; not retried automatically."
    )


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
