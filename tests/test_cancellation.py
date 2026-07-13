from __future__ import annotations

import asyncio
from threading import Event, Thread

import pytest

from paicli.cancellation import CancellationToken, TaskCanceled, await_with_cancellation


def test_cancellation_token_cancels_an_in_flight_async_task():
    token = CancellationToken()
    started = Event()
    cleaned_up = Event()

    async def operation() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned_up.set()

    def cancel_from_another_thread() -> None:
        started.wait(timeout=1)
        token.cancel()

    async def run() -> None:
        task = asyncio.create_task(operation())
        canceller = Thread(target=cancel_from_another_thread)
        canceller.start()
        with pytest.raises(TaskCanceled):
            await await_with_cancellation(task, token)
        canceller.join(timeout=1)
        assert not canceller.is_alive()
        assert task.cancelled()

    asyncio.run(run())
    assert cleaned_up.is_set()
