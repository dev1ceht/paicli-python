from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from paicli.cancellation import TaskCanceled
from paicli.tools.command_runner import CommandRunner


class _ChunkStream:
    def __init__(self, chunks: list[bytes], *, delay: float = 0.0) -> None:
        self._chunks = iter(chunks)
        self._delay = delay

    async def read(self, _size: int) -> bytes:
        if self._delay:
            await asyncio.sleep(self._delay)
        return next(self._chunks, b"")


class _FakeProcess:
    pid = 123

    def __init__(self, stdout: _ChunkStream, stderr: _ChunkStream | None = None) -> None:
        self.returncode = 0
        self.stdout = stdout
        self.stderr = stderr or _ChunkStream([b""])

    async def wait(self) -> int:
        return self.returncode


def test_command_runner_drains_pipe_after_process_exit(monkeypatch, tmp_path):
    process = _FakeProcess(_ChunkStream([b"tail", b""], delay=0.01))

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(
        "paicli.tools.command_runner.asyncio.create_subprocess_exec",
        create_process,
    )
    monkeypatch.setattr("paicli.tools.command_runner.resolve_bash", lambda: "bash")

    result = asyncio.run(CommandRunner(str(tmp_path)).run("printf tail"))

    assert result.output == "tail"
    assert result.exit_code == 0


def test_command_runner_truncates_tail_and_preserves_full_output(monkeypatch, tmp_path):
    payload = b"\n".join(f"line-{index}".encode() for index in range(10)) + b"\n"
    process = _FakeProcess(_ChunkStream([payload, b""]))

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(
        "paicli.tools.command_runner.asyncio.create_subprocess_exec",
        create_process,
    )
    monkeypatch.setattr("paicli.tools.command_runner.resolve_bash", lambda: "bash")

    result = asyncio.run(
        CommandRunner(
            str(tmp_path),
            storage_dir=str(tmp_path / "output"),
            max_lines=3,
            max_bytes=1024,
        ).run("printf output")
    )

    assert result.truncated
    assert result.output.splitlines() == ["line-7", "line-8", "line-9"]
    assert result.full_output_path is not None
    assert Path(result.full_output_path).read_bytes() == payload


def test_command_runner_cancels_process_tree(monkeypatch, tmp_path):
    class WaitingProcess(_FakeProcess):
        def __init__(self) -> None:
            super().__init__(_ChunkStream([b""]))
            self.returncode = None
            self.finished = asyncio.Event()

        async def wait(self) -> int:
            await self.finished.wait()
            return self.returncode or -15

    process = WaitingProcess()
    stopped = asyncio.Event()

    async def create_process(*_args, **_kwargs):
        return process

    async def stop_process_tree(proc):
        proc.returncode = -15
        proc.finished.set()
        stopped.set()

    monkeypatch.setattr(
        "paicli.tools.command_runner.asyncio.create_subprocess_exec",
        create_process,
    )
    monkeypatch.setattr("paicli.tools.command_runner.resolve_bash", lambda: "bash")
    monkeypatch.setattr("paicli.tools.command_runner.stop_process_tree", stop_process_tree)
    canceled = False

    async def run():
        nonlocal canceled

        def is_canceled() -> bool:
            return canceled

        task = asyncio.create_task(
            CommandRunner(str(tmp_path)).run("sleep forever", cancellation_check=is_canceled)
        )
        await asyncio.sleep(0.15)
        canceled = True
        with pytest.raises(TaskCanceled):
            await task

    asyncio.run(run())
    assert stopped.is_set()
