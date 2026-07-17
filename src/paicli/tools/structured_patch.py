from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

BEGIN_MARKER = "*** Begin Patch"
END_MARKER = "*** End Patch"


@dataclass(slots=True)
class PatchHunk:
    old_lines: list[str] = field(default_factory=list)
    new_lines: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PatchOperation:
    action: str
    path: str
    move_to: str | None = None
    add_lines: list[str] = field(default_factory=list)
    hunks: list[PatchHunk] = field(default_factory=list)


class StructuredPatchResult(TypedDict):
    changed_files: list[str]
    created_files: list[str]
    deleted_files: list[str]
    moved_files: list[dict[str, str]]
    dry_run: bool


def apply_structured_patch(
    patch: str,
    *,
    resolve_path: Callable[[str], Path],
    dry_run: bool = False,
) -> StructuredPatchResult:
    operations = _parse_patch(patch)
    writes: list[tuple[Path, str]] = []
    deletes: list[Path] = []
    changed: list[str] = []
    created: list[str] = []
    deleted: list[str] = []
    moved: list[dict[str, str]] = []

    for operation in operations:
        target = resolve_path(operation.path)
        if operation.action == "add":
            if target.exists():
                raise ValueError(f"file already exists: {operation.path}")
            writes.append((target, _join_lines(operation.add_lines)))
            changed.append(operation.path)
            created.append(operation.path)
            continue
        if operation.action == "delete":
            if not target.is_file():
                raise ValueError(f"file does not exist: {operation.path}")
            deletes.append(target)
            changed.append(operation.path)
            deleted.append(operation.path)
            continue
        if not target.is_file():
            raise ValueError(f"file does not exist: {operation.path}")
        destination = resolve_path(operation.move_to) if operation.move_to else target
        if destination != target and destination.exists():
            raise ValueError(f"move destination already exists: {operation.move_to}")
        text = target.read_text(encoding="utf-8")
        for hunk in operation.hunks:
            old = _join_lines(hunk.old_lines)
            new = _join_lines(hunk.new_lines)
            count = text.count(old)
            if count == 0:
                raise ValueError(f"patch context not found in {operation.path}")
            if count > 1:
                raise ValueError(
                    f"patch context matched {count} times in {operation.path}; add more context"
                )
            text = text.replace(old, new, 1)
        writes.append((destination, text))
        changed.append(operation.move_to or operation.path)
        if destination != target:
            deletes.append(target)
            moved.append({"source": operation.path, "destination": operation.move_to or ""})

    if not dry_run:
        for target, text in writes:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        for target in deletes:
            target.unlink()
    return {
        "changed_files": changed,
        "created_files": created,
        "deleted_files": deleted,
        "moved_files": moved,
        "dry_run": dry_run,
    }


def _parse_patch(patch: str) -> list[PatchOperation]:
    lines = patch.splitlines()
    if not lines or lines[0] != BEGIN_MARKER:
        raise ValueError(f"patch must start with {BEGIN_MARKER}")
    if lines[-1] != END_MARKER:
        raise ValueError(f"patch must end with {END_MARKER}")
    operations: list[PatchOperation] = []
    index = 1
    while index < len(lines) - 1:
        line = lines[index]
        if line.startswith("*** Add File: "):
            operation, index = _parse_add(lines, index)
        elif line.startswith("*** Update File: "):
            operation, index = _parse_update(lines, index)
        elif line.startswith("*** Delete File: "):
            path = line.removeprefix("*** Delete File: ").strip()
            if not path:
                raise ValueError("Delete File requires a path")
            operation, index = PatchOperation("delete", path), index + 1
        else:
            raise ValueError(f"unrecognized patch line: {line}")
        operations.append(operation)
    if not operations:
        raise ValueError("patch contains no file operations")
    return operations


def _parse_add(lines: list[str], index: int) -> tuple[PatchOperation, int]:
    path = lines[index].removeprefix("*** Add File: ").strip()
    if not path:
        raise ValueError("Add File requires a path")
    added: list[str] = []
    index += 1
    while index < len(lines) - 1 and not lines[index].startswith("*** "):
        if not lines[index].startswith("+"):
            raise ValueError("Add File content lines must start with +")
        added.append(lines[index][1:])
        index += 1
    return PatchOperation("add", path, add_lines=added), index


def _parse_update(lines: list[str], index: int) -> tuple[PatchOperation, int]:
    path = lines[index].removeprefix("*** Update File: ").strip()
    if not path:
        raise ValueError("Update File requires a path")
    operation = PatchOperation("update", path)
    index += 1
    while index < len(lines) - 1 and (
        not lines[index].startswith("*** ") or lines[index].startswith("*** Move to: ")
    ):
        if lines[index].startswith("*** Move to: "):
            operation.move_to = lines[index].removeprefix("*** Move to: ").strip()
            if not operation.move_to:
                raise ValueError("Move to requires a path")
            index += 1
            continue
        if not lines[index].startswith("@@"):
            raise ValueError("Update File hunks must start with @@")
        hunk, index = _parse_hunk(lines, index + 1)
        operation.hunks.append(hunk)
    if not operation.hunks and operation.move_to is None:
        raise ValueError("Update File requires a hunk or Move to")
    return operation, index


def _parse_hunk(lines: list[str], index: int) -> tuple[PatchHunk, int]:
    hunk = PatchHunk()
    while (
        index < len(lines) - 1
        and not lines[index].startswith("@@")
        and not lines[index].startswith("*** ")
    ):
        line = lines[index]
        if line.startswith("+"):
            hunk.new_lines.append(line[1:])
        elif line.startswith("-"):
            hunk.old_lines.append(line[1:])
        elif line.startswith(" "):
            hunk.old_lines.append(line[1:])
            hunk.new_lines.append(line[1:])
        elif line == "":
            hunk.old_lines.append("")
            hunk.new_lines.append("")
        else:
            raise ValueError(f"invalid patch hunk line: {line}")
        index += 1
    if not hunk.old_lines and not hunk.new_lines:
        raise ValueError("patch hunk cannot be empty")
    return hunk, index


def _join_lines(lines: list[str]) -> str:
    return "" if not lines else "\n".join(lines) + "\n"
