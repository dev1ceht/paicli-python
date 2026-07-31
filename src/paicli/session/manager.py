from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from paicli.clock import now_timestamp


@dataclass(frozen=True, slots=True)
class SessionHeader:
    id: str
    timestamp: str
    cwd: str
    parent_session: str | None = None
    title: str = "New session"
    metadata: dict[str, Any] = field(default_factory=dict)
    version: int = 1

    def to_json(self) -> dict[str, Any]:
        return {
            "type": "session",
            "version": self.version,
            "id": self.id,
            "timestamp": self.timestamp,
            "cwd": self.cwd,
            "parentSession": self.parent_session,
            "title": self.title,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class SessionEntry:
    type: str
    id: str
    parent_id: str | None
    timestamp: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "id": self.id,
            "parentId": self.parent_id,
            "timestamp": self.timestamp,
            **self.data,
        }


class SessionManager:
    """Manage one append-only, tree-shaped JSONL Session."""

    def __init__(
        self,
        path: Path,
        header: SessionHeader,
        entries: list[SessionEntry] | None = None,
    ) -> None:
        self.path = path
        self.header = header
        self._entries = list(entries or ())
        self._by_id = {entry.id: entry for entry in self._entries}
        self._leaf_id = self._entries[-1].id if self._entries else None

    @property
    def id(self) -> str:
        return self.header.id

    @property
    def leaf_id(self) -> str | None:
        return self._leaf_id

    @property
    def updated_at(self) -> str:
        return self._entries[-1].timestamp if self._entries else self.header.timestamp

    @property
    def modified_ns(self) -> int:
        stat = self.path.stat()
        return max(stat.st_mtime_ns, stat.st_ctime_ns)

    def append(self, entry_type: str, data: dict[str, Any] | None = None) -> str:
        entry = SessionEntry(
            type=entry_type,
            id=uuid4().hex[:8],
            parent_id=self._leaf_id,
            timestamp=now_timestamp(),
            data=dict(data or {}),
        )
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(entry.to_json(), ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")
            stream.flush()
        self._entries.append(entry)
        self._by_id[entry.id] = entry
        self._leaf_id = entry.id
        return entry.id

    def entries(self) -> tuple[SessionEntry, ...]:
        return tuple(self._entries)

    def branch(self, entry_id: str | None) -> None:
        if entry_id is not None and entry_id not in self._by_id:
            raise KeyError(f"Session Entry not found: {entry_id}")
        self._leaf_id = entry_id

    def current_branch(self) -> tuple[SessionEntry, ...]:
        branch: list[SessionEntry] = []
        entry = self._by_id.get(self._leaf_id) if self._leaf_id is not None else None
        while entry is not None:
            branch.append(entry)
            entry = self._by_id.get(entry.parent_id) if entry.parent_id is not None else None
        branch.reverse()
        return tuple(branch)


def load_session(path: Path) -> SessionManager:
    records: list[dict[str, Any]] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    last_content_index = next(
        (index for index in range(len(lines) - 1, -1, -1) if lines[index].strip()),
        -1,
    )
    for index, raw_line in enumerate(lines):
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            if index == last_content_index:
                break
            raise ValueError(f"Malformed Session Entry at line {index + 1}: {path}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"Session Entry must be an object at line {index + 1}: {path}")
        records.append(record)
    if not records or records[0].get("type") != "session":
        raise ValueError(f"Session Header missing: {path}")
    raw_header = records[0]
    header = SessionHeader(
        id=str(raw_header["id"]),
        timestamp=str(raw_header["timestamp"]),
        cwd=str(raw_header["cwd"]),
        parent_session=_optional_string(raw_header.get("parentSession")),
        title=str(raw_header.get("title") or "New session"),
        metadata=dict(raw_header.get("metadata") or {}),
        version=int(raw_header.get("version") or 1),
    )
    entries: list[SessionEntry] = []
    seen_ids: set[str] = set()
    for index, record in enumerate(records[1:], start=2):
        if not all(key in record for key in ("type", "id", "timestamp")):
            raise ValueError(f"Invalid Session Entry at line {index}: {path}")
        entry_id = str(record["id"])
        parent_id = _optional_string(record.get("parentId"))
        if entry_id in seen_ids:
            raise ValueError(f"Duplicate Session Entry id at line {index}: {path}")
        if parent_id is not None and parent_id not in seen_ids:
            raise ValueError(f"Unknown parentId at line {index}: {path}")
        entries.append(
            SessionEntry(
                type=str(record["type"]),
                id=entry_id,
                parent_id=parent_id,
                timestamp=str(record["timestamp"]),
                data={
                    key: value
                    for key, value in record.items()
                    if key not in {"type", "id", "parentId", "timestamp"}
                },
            )
        )
        seen_ids.add(entry_id)
    return SessionManager(path, header, entries)


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None
