from __future__ import annotations

import asyncio
import codecs
import locale
import os
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from paicli.cancellation import TaskCanceled

DEFAULT_MAX_OUTPUT_LINES = 2000
DEFAULT_MAX_OUTPUT_BYTES = 50 * 1024
_READ_CHUNK_SIZE = 4096


@dataclass(slots=True)
class CommandResult:
    output: str
    exit_code: int | None
    timed_out: bool = False
    full_output_path: str | None = None
    truncated: bool = False


class _OutputAccumulator:
    """Keep a bounded tail while preserving the complete output on disk."""

    def __init__(
        self,
        *,
        storage_dir: str | None,
        max_lines: int = DEFAULT_MAX_OUTPUT_LINES,
        max_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        self.max_lines = max_lines
        self.max_bytes = max_bytes
        self._storage_dir = storage_dir
        self._all_chunks: list[bytes] = []
        self._tail = ""
        self._tail_bytes = 0
        self._total_bytes = 0
        self._completed_lines = 0
        self._has_open_line = False
        self._temp_path: str | None = None
        self._temp_handle = None

    def append(self, data: bytes, text: str) -> None:
        if not data:
            return
        self._total_bytes += len(data)
        self._append_text(text)

        if self._temp_handle is not None:
            self._temp_handle.write(data)
        elif self._is_truncated():
            self._ensure_temp_file()
            if self._temp_handle is not None:
                self._temp_handle.write(data)
            else:
                self._all_chunks.append(data)
        else:
            self._all_chunks.append(data)

    def append_decoded(self, text: str) -> None:
        if not text:
            return
        self._append_text(text)

    def _append_text(self, text: str) -> None:
        normalized = text.replace("\r", "")
        if not normalized:
            return
        self._completed_lines += normalized.count("\n")
        self._has_open_line = not normalized.endswith("\n")
        self._tail += normalized
        self._tail_bytes = len(self._tail.encode("utf-8"))
        if self._tail_bytes > self.max_bytes * 2:
            self._trim_tail()

    def finish(self) -> None:
        if self._temp_handle is not None:
            self._temp_handle.flush()
            self._temp_handle.close()
            self._temp_handle = None

    def snapshot(self) -> tuple[str, bool, str | None]:
        truncated = self._is_truncated()
        content = self._tail
        if truncated:
            content = _truncate_tail(content, max_lines=self.max_lines, max_bytes=self.max_bytes)
        return content, truncated, self._temp_path

    def _is_truncated(self) -> bool:
        total_lines = self._completed_lines + int(self._has_open_line)
        return self._total_bytes > self.max_bytes or total_lines > self.max_lines

    def _trim_tail(self) -> None:
        encoded = self._tail.encode("utf-8")
        if len(encoded) <= self.max_bytes * 2:
            return
        start = len(encoded) - self.max_bytes * 2
        while start < len(encoded) and (encoded[start] & 0xC0) == 0x80:
            start += 1
        self._tail = encoded[start:].decode("utf-8", errors="replace")
        self._tail_bytes = len(self._tail.encode("utf-8"))

    def _ensure_temp_file(self) -> None:
        if self._temp_path is not None:
            return
        directory: str | None = None
        if self._storage_dir:
            try:
                Path(self._storage_dir).mkdir(parents=True, exist_ok=True)
                directory = self._storage_dir
            except OSError:
                directory = None
        try:
            descriptor, path = tempfile.mkstemp(
                prefix="paicli-bash-",
                suffix=".log",
                dir=directory,
            )
            handle = os.fdopen(descriptor, "wb")
        except OSError:
            return
        self._temp_path = path
        self._temp_handle = handle
        for chunk in self._all_chunks:
            handle.write(chunk)
        self._all_chunks.clear()


def _truncate_tail(text: str, *, max_lines: int, max_bytes: int) -> str:
    lines = text.splitlines()
    if len(lines) > max_lines:
        text = "\n".join(lines[-max_lines:])
    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        encoded = encoded[-max_bytes:]
        while encoded and (encoded[0] & 0xC0) == 0x80:
            encoded = encoded[1:]
        text = encoded.decode("utf-8", errors="replace")
        if "\n" in text:
            text = text.split("\n", 1)[1]
    return text


def resolve_bash() -> str:
    candidates: list[str | Path] = []
    executable = shutil.which("bash")
    if executable:
        candidates.append(executable)
    if os.name == "nt":
        candidates.extend(
            Path(entry) / "bash.exe"
            for entry in os.environ.get("PATH", "").split(os.pathsep)
            if entry
        )
        candidates.extend(
            [
                Path(os.environ.get("PROGRAMW6432", "")) / "Git" / "bin" / "bash.exe",
                Path(os.environ.get("PROGRAMFILES", "")) / "Git" / "bin" / "bash.exe",
                Path(os.environ.get("PROGRAMFILES", "")) / "Git" / "usr" / "bin" / "bash.exe",
            ]
        )
    for candidate in candidates:
        try:
            path = Path(candidate)
            if not path.is_file():
                continue
            if os.name == "nt" and path.resolve().parent.name.lower() == "system32":
                continue
            return str(path)
        except OSError:
            continue
    raise FileNotFoundError("bash executable was not found; install Bash or Git for Windows")


def _shell_encoding() -> str:
    if os.name != "nt":
        return "utf-8"
    encodings: list[str] = []
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        for getter_name in ("GetConsoleOutputCP", "GetOEMCP"):
            code_page = getattr(kernel32, getter_name)()
            if code_page:
                encodings.append(f"cp{code_page}")
    except (AttributeError, OSError):
        pass
    encodings.append(locale.getpreferredencoding(False))
    for encoding in dict.fromkeys(encodings):
        try:
            "".encode(encoding)
        except LookupError:
            continue
        return encoding
    return "utf-8"


def _stop_process_tree_sync_options() -> dict[str, int | bool]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def stop_process_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    if os.name == "nt":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(proc.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        except OSError:
            with suppress(ProcessLookupError):
                proc.kill()
    else:
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
    except TimeoutError:
        if os.name != "nt":
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        else:
            with suppress(ProcessLookupError):
                proc.kill()
        with suppress(TimeoutError):
            with asyncio.timeout(3):
                await proc.wait()


class CommandRunner:
    """Run non-interactive Bash commands with bounded, observable output."""

    def __init__(
        self,
        cwd: str,
        *,
        storage_dir: str | None = None,
        max_lines: int = DEFAULT_MAX_OUTPUT_LINES,
        max_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        self.cwd = cwd
        self.storage_dir = storage_dir
        self.max_lines = max_lines
        self.max_bytes = max_bytes

    async def run(
        self,
        command: str,
        *,
        timeout: float | None = None,
        on_output: Callable[[str, str], None] | None = None,
        cancellation_check: Callable[[], bool] | None = None,
    ) -> CommandResult:
        executable = resolve_bash()
        process = await asyncio.create_subprocess_exec(
            executable,
            "--noprofile",
            "--norc",
            "-c",
            command,
            cwd=self.cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
            **_stop_process_tree_sync_options(),
        )
        accumulator = _OutputAccumulator(
            storage_dir=self.storage_dir,
            max_lines=self.max_lines,
            max_bytes=self.max_bytes,
        )
        encoding = _shell_encoding()
        decoders = {
            "stdout": codecs.getincrementaldecoder(encoding)(errors="replace"),
            "stderr": codecs.getincrementaldecoder(encoding)(errors="replace"),
        }

        async def consume(stream: asyncio.StreamReader | None, stream_name: str) -> None:
            if stream is None:
                return
            while True:
                chunk = await stream.read(_READ_CHUNK_SIZE)
                if not chunk:
                    tail = decoders[stream_name].decode(b"", final=True)
                    if tail:
                        accumulator.append_decoded(tail)
                        if on_output:
                            on_output(tail, stream_name)
                    return
                text = decoders[stream_name].decode(chunk)
                accumulator.append(chunk, text)
                if text and on_output:
                    on_output(text, stream_name)

        readers = [
            asyncio.create_task(consume(process.stdout, "stdout")),
            asyncio.create_task(consume(process.stderr, "stderr")),
        ]
        waiter = asyncio.create_task(process.wait())
        timed_out = False
        try:
            deadline = asyncio.get_running_loop().time() + timeout if timeout is not None else None
            while not waiter.done():
                if cancellation_check and cancellation_check():
                    await stop_process_tree(process)
                    raise TaskCanceled()
                wait_for = 0.1
                if deadline is not None:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        timed_out = True
                        await stop_process_tree(process)
                        break
                    wait_for = min(wait_for, remaining)
                try:
                    await asyncio.wait_for(asyncio.shield(waiter), timeout=wait_for)
                except TimeoutError:
                    continue
            if not timed_out:
                await waiter
        except asyncio.CancelledError:
            await stop_process_tree(process)
            raise
        finally:
            if process.returncode is None:
                await stop_process_tree(process)
            if not waiter.done():
                waiter.cancel()
                with suppress(asyncio.CancelledError):
                    await waiter
            try:
                await asyncio.wait_for(
                    asyncio.gather(*readers, return_exceptions=True),
                    timeout=3,
                )
            except TimeoutError:
                for reader in readers:
                    if not reader.done():
                        reader.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
            accumulator.finish()

        output, truncated, full_output_path = accumulator.snapshot()
        return CommandResult(
            output=output,
            exit_code=process.returncode,
            timed_out=timed_out,
            full_output_path=full_output_path,
            truncated=truncated,
        )
