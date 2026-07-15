from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from paicli.context.telemetry import use_context_scope
from paicli.llm.base import LlmClient
from paicli.types import Message


@dataclass(slots=True)
class MemoryEntry:
    id: str
    content: str
    type: str = "FACT"
    timestamp: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    tokenCount: int = 0

    @property
    def scope(self) -> str:
        return "project" if self.metadata.get("scope", "").lower() == "project" else "global"

    @property
    def created_at(self) -> str:
        return self.timestamp


@dataclass(slots=True)
class PendingMemoryChange:
    id: str
    operation: str
    scope: str
    target_memory_ids: list[str]
    proposed_content: str
    reason: str
    source_fact: str
    timestamp: str
    project: str = ""


@dataclass(slots=True)
class MemorySaveResult:
    status: str
    memory_id: str = ""
    change_id: str = ""


class MemoryManager:
    def __init__(
        self,
        storage_path: str | Path,
        project_path: str | Path | None = None,
        *,
        scope: str | Path | None = None,
    ):
        self.storage_path = Path(storage_path).expanduser()
        self.pending_path = self.storage_path.with_name("pending_changes.json")
        self.project_path = _normalize_project_path(project_path or scope or Path.cwd())
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_file()
        if not self.pending_path.exists():
            self.pending_path.write_text("[]", encoding="utf-8")

    def save(
        self,
        content: str,
        *,
        scope: str = "project",
        project_path: str | Path | None = None,
        timestamp: datetime | str | None = None,
    ) -> str:
        text = content.strip()
        if not text:
            raise ValueError("memory content cannot be empty")
        normalized_scope = "global" if scope.lower() == "global" else "project"
        metadata = {"source": "fact", "scope": normalized_scope}
        if normalized_scope == "project":
            metadata["project"] = _normalize_project_path(project_path or self.project_path)

        entries = self._load()
        for entry in entries:
            if entry.content == text and entry.metadata == metadata:
                return entry.id

        entry = MemoryEntry(
            id=f"fact-{uuid4().hex[:8]}",
            content=text,
            type="FACT",
            timestamp=_format_timestamp(timestamp),
            metadata=metadata,
            tokenCount=estimate_tokens(text),
        )
        entries.append(entry)
        self._save(entries)
        return entry.id

    async def save_with_classification(
        self, content: str, *, scope: str, llm_client: LlmClient | None
    ) -> MemorySaveResult:
        candidates = self.search(content, limit=5)
        if not candidates or llm_client is None:
            return MemorySaveResult("saved", memory_id=self.save(content, scope=scope))
        try:
            response = ""
            prompt = json.dumps({"fact": content, "candidates": [
                {"id": item.id, "content": item.content} for item in candidates
            ]}, ensure_ascii=False)
            with use_context_scope(None):
                async for event in llm_client.chat([Message(role="user", content=prompt)], [], system_prompt=(
                    "Classify the fact against candidates. Return JSON only with relationship "
                    "(duplicate, merge, replace, independent), target_memory_ids, proposed_content, reason."
                )):
                    if event.get("type") == "text_delta":
                        response += str(event.get("text") or "")
            result = json.loads(response)
            relationship = str(result.get("relationship") or "independent")
            targets = [str(value) for value in result.get("target_memory_ids") or []]
            if relationship == "duplicate" and targets:
                return MemorySaveResult("duplicate", memory_id=targets[0])
            if relationship in {"merge", "replace"} and targets:
                change = self.propose_change(operation=relationship, target_memory_ids=targets,
                    proposed_content=str(result.get("proposed_content") or content),
                    reason=str(result.get("reason") or "Related memory"), source_fact=content, scope=scope)
                return MemorySaveResult("pending", change_id=change.id)
        except Exception:
            pass
        return MemorySaveResult("saved", memory_id=self.save(content, scope=scope))

    def list(self, limit: int = 20, *, visible_only: bool = True) -> list[MemoryEntry]:
        entries = self._load()
        if visible_only:
            entries = [entry for entry in entries if self.is_visible(entry)]
        return entries[:limit]

    def search(self, query: str, limit: int = 10) -> list[MemoryEntry]:
        normalized = query.lower().strip()
        if not normalized:
            return self.list(limit)
        terms = tokenize(normalized)
        scored: list[tuple[float, MemoryEntry]] = []
        for entry in self._load():
            if not self.is_visible(entry):
                continue
            score = self._score(entry, normalized, terms)
            if score > 0:
                scored.append((score, entry))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [entry for _score, entry in scored[:limit]]

    def build_context_for_query(self, query: str, max_tokens: int) -> str:
        relevant = self.search(query, limit=10)
        if not relevant:
            return ""
        lines = ["## 相关长期记忆", ""]
        used_tokens = 0
        for entry in relevant:
            line = f"- [{entry.type}] {entry.content}"
            cost = estimate_tokens(line)
            if used_tokens + cost > max_tokens:
                break
            lines.append(line)
            used_tokens += cost
        if len(lines) == 2:
            return ""
        return "\n".join(lines) + "\n"

    def delete(self, memory_id: str) -> bool:
        entries = self._load()
        remaining = [entry for entry in entries if entry.id != memory_id]
        if len(remaining) == len(entries):
            return False
        self._save(remaining)
        return True

    def clear(self) -> int:
        entries = self._load()
        self._save([])
        return len(entries)

    def propose_change(
        self,
        *,
        operation: str,
        target_memory_ids: list[str],
        proposed_content: str,
        reason: str,
        source_fact: str,
        scope: str = "project",
    ) -> PendingMemoryChange:
        if operation not in {"merge", "replace", "retire"}:
            raise ValueError("unsupported pending memory operation")
        change = PendingMemoryChange(
            id=f"change-{uuid4().hex[:8]}",
            operation=operation,
            scope=scope,
            target_memory_ids=target_memory_ids,
            proposed_content=proposed_content.strip(),
            reason=reason.strip(),
            source_fact=source_fact.strip(),
            timestamp=_format_timestamp(None),
            project=self.project_path if scope == "project" else "",
        )
        changes = self.list_pending(visible_only=False)
        changes.append(change)
        self._save_pending(changes)
        return change

    def list_pending(self, *, visible_only: bool = True) -> list[PendingMemoryChange]:
        try:
            raw = json.loads(self.pending_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        changes = [_pending_from_dict(item) for item in raw if isinstance(item, dict)]
        values = [change for change in changes if change]
        if visible_only:
            values = [
                change
                for change in values
                if change.scope == "global" or change.project == self.project_path
            ]
        return values

    def reject_pending(self, change_id: str) -> bool:
        visible = self.list_pending()
        if not any(change.id == change_id for change in visible):
            return False
        changes = self.list_pending(visible_only=False)
        self._save_pending([change for change in changes if change.id != change_id])
        return True

    def apply_pending(self, change_id: str) -> str | None:
        change = next((item for item in self.list_pending() if item.id == change_id), None)
        if not change:
            return None
        changes = self.list_pending(visible_only=False)
        entries = self._load()
        if change.operation == "retire":
            entries = [entry for entry in entries if entry.id not in change.target_memory_ids]
            result_id = "retired"
        else:
            entries = [entry for entry in entries if entry.id not in change.target_memory_ids]
            metadata = {"source": "fact", "scope": change.scope}
            if change.scope == "project":
                metadata["project"] = change.project
            entry = MemoryEntry(
                id=f"fact-{uuid4().hex[:8]}",
                content=change.proposed_content,
                type="FACT",
                timestamp=_format_timestamp(None),
                metadata=metadata,
                tokenCount=estimate_tokens(change.proposed_content),
            )
            entries.append(entry)
            result_id = entry.id
        self._save(entries)
        self._save_pending([item for item in changes if item.id != change_id])
        return result_id

    def status(self) -> str:
        entries = self._load()
        facts = sum(1 for entry in entries if entry.type == "FACT")
        summaries = sum(1 for entry in entries if entry.type == "SUMMARY")
        tool_results = sum(1 for entry in entries if entry.type == "TOOL_RESULT")
        tokens = sum(entry.tokenCount for entry in entries)
        return (
            f"长期记忆: {len(entries)}条 / {tokens} tokens "
            f"(事实: {facts}, 摘要: {summaries}, 工具结果: {tool_results})"
        )

    def is_visible(self, entry: MemoryEntry) -> bool:
        if entry.scope == "global":
            return True
        return entry.metadata.get("project") == self.project_path

    def _score(self, entry: MemoryEntry, query: str, terms: set[str]) -> float:
        content = entry.content.lower()
        if query and query in content:
            relevance = 2.0
        else:
            matched = sum(1 for term in terms if term and term in content)
            if matched == 0:
                return 0.0
            relevance = matched / max(len(terms), 1)
        return relevance * _recency_decay(entry.timestamp) * 1.2

    def _ensure_file(self) -> None:
        if not self.storage_path.exists():
            self.storage_path.write_text("[]", encoding="utf-8")

    def _load(self) -> list[MemoryEntry]:
        try:
            raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(raw, list):
            return []
        entries = []
        for item in raw:
            if isinstance(item, dict):
                entry = _entry_from_dict(item)
                if entry:
                    entries.append(entry)
        return entries

    def _save(self, entries: list[MemoryEntry]) -> None:
        data = [
            {
                "id": entry.id,
                "content": entry.content,
                "type": entry.type,
                "timestamp": entry.timestamp,
                "metadata": entry.metadata,
                "tokenCount": entry.tokenCount,
            }
            for entry in entries
        ]
        self.storage_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _save_pending(self, changes: list[PendingMemoryChange]) -> None:
        self.pending_path.write_text(
            json.dumps(
                [
                    {
                        "id": item.id,
                        "status": "pending",
                        "operation": item.operation,
                        "scope": item.scope,
                        "project": item.project,
                        "target_memory_ids": item.target_memory_ids,
                        "proposed_content": item.proposed_content,
                        "reason": item.reason,
                        "source_fact": item.source_fact,
                        "created_at": item.timestamp,
                    }
                    for item in changes
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    chinese_chars = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other_chars = len(text) - chinese_chars
    return math.ceil(chinese_chars / 1.5 + other_chars / 4.0)


def tokenize(text: str) -> set[str]:
    normalized = text.lower().strip()
    parts = {
        part.strip()
        for part in re.split(r"[\s,，。！？；;:：()（）\[\]【】\"']+", normalized)
        if part.strip()
    }
    if normalized:
        parts.add(normalized)
    return parts


def _entry_from_dict(item: dict[str, Any]) -> MemoryEntry | None:
    content = str(item.get("content") or "").strip()
    if not content:
        return None
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    clean_metadata = {str(key): str(value) for key, value in metadata.items()}
    token_count = item.get("tokenCount")
    return MemoryEntry(
        id=str(item.get("id") or f"fact-{uuid4().hex[:8]}"),
        content=content,
        type=str(item.get("type") or "FACT"),
        timestamp=str(item.get("timestamp") or datetime.now(UTC).isoformat()),
        metadata=clean_metadata,
        tokenCount=(
            int(token_count) if isinstance(token_count, int | float) else estimate_tokens(content)
        ),
    )


def _pending_from_dict(item: dict[str, Any]) -> PendingMemoryChange | None:
    if str(item.get("status") or "pending") != "pending":
        return None
    operation = str(item.get("operation") or "")
    if operation not in {"merge", "replace", "retire"}:
        return None
    targets = item.get("target_memory_ids")
    if not isinstance(targets, list):
        return None
    return PendingMemoryChange(
        id=str(item.get("id") or f"change-{uuid4().hex[:8]}"),
        operation=operation,
        scope="global" if str(item.get("scope") or "").lower() == "global" else "project",
        target_memory_ids=[str(value) for value in targets],
        proposed_content=str(item.get("proposed_content") or ""),
        reason=str(item.get("reason") or ""),
        source_fact=str(item.get("source_fact") or ""),
        timestamp=str(item.get("created_at") or ""),
        project=str(item.get("project") or ""),
    )


def _format_timestamp(value: datetime | str | None) -> str:
    if isinstance(value, datetime):
        candidate = value if value.tzinfo else value.replace(tzinfo=UTC)
        return candidate.astimezone(UTC).isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return datetime.now(UTC).isoformat()


def _recency_decay(timestamp: str) -> float:
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return 0.5
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    age_hours = (datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds() / 3600
    return max(0.5, 1.0 - age_hours / 24.0)


def _normalize_project_path(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())
