from __future__ import annotations

import json
from pathlib import Path


class PromptHistory:
    """Persistent bounded prompt history with cursor navigation."""

    def __init__(self, path: Path, limit: int = 200) -> None:
        self.path = path
        self.limit = limit
        self._items = self._load_items()[-limit:]
        self._cursor = len(self._items)

    def _load_items(self) -> list[str]:
        if not self.path.exists():
            return []
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        items: list[str] = []
        for line in raw.splitlines():
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                value = line
            if isinstance(value, str) and value:
                items.append(value)
        return items

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in self._items)
        self.path.write_text(payload, encoding="utf-8")

    def append(self, text: str) -> None:
        if text and (not self._items or self._items[-1] != text):
            self._items = (self._items + [text])[-self.limit :]
            self._persist()
        self.reset_cursor()

    def previous(self) -> str:
        if not self._items:
            return ""
        self._cursor = max(0, self._cursor - 1)
        return self._items[self._cursor]

    def next(self) -> str:
        if not self._items:
            return ""
        self._cursor = min(len(self._items), self._cursor + 1)
        if self._cursor >= len(self._items):
            return ""
        return self._items[self._cursor]

    def reset_cursor(self) -> None:
        self._cursor = len(self._items)
