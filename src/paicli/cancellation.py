from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from contextlib import suppress
from typing import TypeVar

CancellationCheck = Callable[[], bool]
T = TypeVar("T")


class TaskCanceled(Exception):
    """Raised when a background task reaches a cooperative cancellation boundary."""


def raise_if_cancelled(cancellation_check: CancellationCheck | None) -> None:
    if cancellation_check and cancellation_check():
        raise TaskCanceled


class CancellationToken:
    """A cancellation signal that can be triggered outside an asyncio event loop."""

    def __init__(self) -> None:
        self._thread_event = threading.Event()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async_event: asyncio.Event | None = None

    def is_set(self) -> bool:
        return self._thread_event.is_set()

    def cancel(self) -> None:
        self._thread_event.set()
        with self._lock:
            loop = self._loop
            async_event = self._async_event
        if loop and async_event:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(async_event.set)

    async def wait(self) -> None:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._async_event is None:
                self._loop = loop
                self._async_event = asyncio.Event()
            elif self._loop is not loop:
                raise RuntimeError("cancellation token cannot be shared across event loops")
            async_event = self._async_event
            if self._thread_event.is_set():
                async_event.set()
        await async_event.wait()


async def await_with_cancellation(
    operation: asyncio.Task[T],
    token: CancellationToken,
) -> T:
    cancellation_waiter = asyncio.create_task(token.wait())
    try:
        done, _pending = await asyncio.wait(
            {operation, cancellation_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation_waiter in done:
            operation.cancel()
            with suppress(asyncio.CancelledError):
                await operation
            raise TaskCanceled
        return await operation
    finally:
        cancellation_waiter.cancel()
        with suppress(asyncio.CancelledError):
            await cancellation_waiter
