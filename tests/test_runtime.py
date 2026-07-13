from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from paicli.cancellation import CancellationToken
from paicli.config import load_config
from paicli.runtime import DurableTaskManager, RuntimeApiServer


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
