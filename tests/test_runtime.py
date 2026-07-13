from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from paicli.runtime import DurableTaskManager


def test_durable_task_lifecycle(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = manager.add("do work")

    task = manager.claim_next()
    assert task is not None
    assert task.id == task_id
    assert task.status == "running"

    assert manager.complete(task_id, "done")
    completed = manager.get(task_id)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.result == "done"


def test_durable_task_cancel(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = manager.add("do work")

    assert manager.cancel(task_id)
    assert manager.get(task_id).status == "canceled"  # type: ignore[union-attr]


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
