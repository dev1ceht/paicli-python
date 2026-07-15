"""Recoverability-aware reduction and lifecycle management for tool results."""

from __future__ import annotations

import hashlib
import re
import shutil
import time
from pathlib import Path

from paicli.types import Message

COMPRESSED_PREFIX = "[tool-result-compressed"
OFFLOADED_PREFIX = "[tool-result-offloaded"
COMPRESSED_PLACEHOLDER = (
    "[tool-result-compressed; tool_call_id={tool_call_id}; rerun the tool if needed]"
)
OFFLOADED_PLACEHOLDER = (
    "[tool-result-offloaded; path={file_path}; preview={preview}]"
)
SESSION_MARKER = ".paicli-tool-results"
_SESSION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def is_compressed_tool_result(message: Message) -> bool:
    return message.role == "tool" and str(message.content).startswith(COMPRESSED_PREFIX)


def is_offloaded_tool_result(message: Message) -> bool:
    return message.role == "tool" and str(message.content).startswith(OFFLOADED_PREFIX)


def is_inline_tool_result(message: Message) -> bool:
    return (
        message.role == "tool"
        and not is_compressed_tool_result(message)
        and not is_offloaded_tool_result(message)
    )


def compress_next_old_tool_result(
    messages: list[Message],
    *,
    keep_recent: int = 5,
) -> tuple[list[Message], bool]:
    """Lossily replace one oldest eligible inline result, preserving recent results."""
    tool_indices = [index for index, message in enumerate(messages) if message.role == "tool"]
    protected = set(tool_indices[-max(0, keep_recent) :]) if keep_recent else set()
    for index in tool_indices:
        message = messages[index]
        if index in protected or not is_inline_tool_result(message):
            continue
        result = list(messages)
        result[index] = Message(
            role="tool",
            content=COMPRESSED_PLACEHOLDER.format(
                tool_call_id=message.tool_call_id or "unknown"
            ),
            tool_call_id=message.tool_call_id,
        )
        return result, True
    return messages, False


def compress_old_tool_results(
    messages: list[Message],
    *,
    keep_recent: int = 5,
) -> list[Message]:
    result = list(messages)
    while True:
        result, changed = compress_next_old_tool_result(result, keep_recent=keep_recent)
        if not changed:
            return result


def offload_next_tool_result(
    messages: list[Message],
    *,
    max_total_bytes: int = 200 * 1024,
    preview_chars: int = 200,
    storage_dir: str = "~/.paicli/tool_results",
    session_id: str = "default",
    force: bool = False,
) -> tuple[list[Message], bool]:
    """Offload one largest inline result when the byte threshold is exceeded."""
    inline = [
        (index, message)
        for index, message in enumerate(messages)
        if is_inline_tool_result(message)
    ]
    if not inline:
        return messages, False
    total_bytes = sum(len(str(message.content).encode("utf-8")) for _, message in inline)
    if not force and total_bytes <= max_total_bytes:
        return messages, False

    index, message = max(
        inline,
        key=lambda item: len(str(item[1].content).encode("utf-8")),
    )
    storage_path = Path(storage_dir).expanduser().resolve() / session_id
    try:
        storage_path.mkdir(parents=True, exist_ok=True)
        (storage_path / SESSION_MARKER).touch(exist_ok=True)
    except OSError:
        return messages, False
    identity = f"{message.tool_call_id or 'tool'}:{index}"
    safe_name = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    file_path = storage_path / f"{safe_name}.txt"
    try:
        file_path.write_text(str(message.content), encoding="utf-8")
    except OSError:
        return messages, False

    preview = str(message.content)[:preview_chars].replace("\n", " ").strip()
    if len(str(message.content)) > preview_chars:
        preview += "..."
    placeholder = OFFLOADED_PLACEHOLDER.format(file_path=file_path, preview=preview)
    result = list(messages)
    result[index] = Message(
        role="tool",
        content=placeholder,
        tool_call_id=message.tool_call_id,
    )
    return result, True


def offload_large_tool_results(
    messages: list[Message],
    *,
    max_total_bytes: int = 200 * 1024,
    preview_chars: int = 200,
    storage_dir: str = "~/.paicli/tool_results",
    session_id: str = "default",
) -> list[Message]:
    result = list(messages)
    while True:
        result, changed = offload_next_tool_result(
            result,
            max_total_bytes=max_total_bytes,
            preview_chars=preview_chars,
            storage_dir=storage_dir,
            session_id=session_id,
        )
        if not changed:
            return result


def apply_tool_result_compression(
    messages: list[Message],
    *,
    keep_recent: int = 5,
    max_total_bytes: int = 200 * 1024,
    preview_chars: int = 200,
    storage_dir: str = "~/.paicli/tool_results",
    session_id: str = "default",
) -> list[Message]:
    """Compatibility helper: offload recoverably before lossy old-result compression."""
    result = offload_large_tool_results(
        messages,
        max_total_bytes=max_total_bytes,
        preview_chars=preview_chars,
        storage_dir=storage_dir,
        session_id=session_id,
    )
    return compress_old_tool_results(result, keep_recent=keep_recent)


def cleanup_session_tool_results(storage_dir: str, session_id: str) -> None:
    root = Path(storage_dir).expanduser().resolve()
    target = (root / session_id).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return
    is_owned = _SESSION_ID_PATTERN.fullmatch(session_id) or (
        target / SESSION_MARKER
    ).is_file()
    if is_owned and target != root and target.is_dir() and not target.is_symlink():
        shutil.rmtree(target, ignore_errors=True)


def cleanup_stale_tool_results(
    storage_dir: str,
    *,
    max_age_days: int = 7,
    now: float | None = None,
) -> None:
    root = Path(storage_dir).expanduser().resolve()
    if not root.is_dir():
        return
    cutoff = (time.time() if now is None else now) - max_age_days * 24 * 60 * 60
    for child in root.iterdir():
        try:
            is_session = bool(_SESSION_ID_PATTERN.fullmatch(child.name))
            is_owned = (child / SESSION_MARKER).is_file()
            if (
                is_session
                and is_owned
                and child.is_dir()
                and not child.is_symlink()
                and child.stat().st_mtime < cutoff
            ):
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue
