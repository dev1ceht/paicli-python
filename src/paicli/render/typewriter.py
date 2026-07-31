from __future__ import annotations

import time
from collections import deque
from threading import Lock


class TypewriterBuffer:
    """Thread-safe adaptive display buffer for streamed text."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        chars_per_second: float = 80,
        max_chars_per_second: float = 320,
        frame_rate: float = 30,
    ) -> None:
        if chars_per_second <= 0:
            raise ValueError("chars_per_second must be positive")
        if max_chars_per_second < chars_per_second:
            raise ValueError("max_chars_per_second must be at least chars_per_second")
        if frame_rate <= 0:
            raise ValueError("frame_rate must be positive")
        self.enabled = enabled
        self.chars_per_second = float(chars_per_second)
        self.max_chars_per_second = float(max_chars_per_second)
        self.frame_rate = float(frame_rate)
        self._pending: deque[str] = deque()
        self._pending_length = 0
        self._visible: list[str] = []
        self._credit = 0.0
        self._finish_rate = 0.0
        self._last_tick: float | None = None
        self._lock = Lock()

    @property
    def pending_length(self) -> int:
        with self._lock:
            return self._pending_length

    @property
    def visible_text(self) -> str:
        with self._lock:
            return "".join(self._visible)

    def feed(self, text: str) -> str:
        value = str(text or "")
        if not value:
            return ""
        with self._lock:
            if not self.enabled:
                self._visible.append(value)
                return value
            self._pending.append(value)
            self._pending_length += len(value)
        return ""

    def advance(self, elapsed_seconds: float | None = None) -> str:
        with self._lock:
            if not self._pending_length:
                self._last_tick = time.monotonic()
                return ""
            elapsed = self._elapsed(elapsed_seconds)
            self._credit += elapsed * self._current_rate()
            count = min(self._pending_length, int(self._credit))
            if count <= 0:
                return ""
            self._credit -= count
            chunk = self._take(count)
            self._visible.append(chunk)
            if not self._pending_length:
                self._finish_rate = 0.0
            return chunk

    def finish(self, *, within_seconds: float = 0.25) -> None:
        if within_seconds <= 0:
            raise ValueError("within_seconds must be positive")
        with self._lock:
            if self._pending_length:
                self._finish_rate = max(
                    self._finish_rate,
                    self._pending_length / within_seconds,
                )

    def flush(self) -> str:
        with self._lock:
            if not self._pending_length:
                return ""
            chunk = self._take(self._pending_length)
            self._visible.append(chunk)
            self._credit = 0.0
            self._finish_rate = 0.0
            self._last_tick = None
            return chunk

    def reset(self) -> None:
        with self._lock:
            self._pending.clear()
            self._pending_length = 0
            self._visible.clear()
            self._credit = 0.0
            self._finish_rate = 0.0
            self._last_tick = None

    def _elapsed(self, elapsed_seconds: float | None) -> float:
        if elapsed_seconds is not None:
            if elapsed_seconds < 0:
                raise ValueError("elapsed_seconds cannot be negative")
            self._last_tick = time.monotonic()
            return elapsed_seconds
        now = time.monotonic()
        elapsed = 1 / self.frame_rate if self._last_tick is None else now - self._last_tick
        self._last_tick = now
        return elapsed

    def _current_rate(self) -> float:
        if self._pending_length <= 40:
            adaptive_rate = self.chars_per_second
        elif self._pending_length >= 200:
            adaptive_rate = self.max_chars_per_second
        else:
            progress = (self._pending_length - 40) / 160
            adaptive_rate = (
                self.chars_per_second
                + (self.max_chars_per_second - self.chars_per_second) * progress
            )
        return max(adaptive_rate, self._finish_rate)

    def _take(self, count: int) -> str:
        parts: list[str] = []
        remaining = count
        while remaining:
            value = self._pending.popleft()
            if len(value) <= remaining:
                parts.append(value)
                remaining -= len(value)
                continue
            parts.append(value[:remaining])
            self._pending.appendleft(value[remaining:])
            remaining = 0
        self._pending_length -= count
        return "".join(parts)
