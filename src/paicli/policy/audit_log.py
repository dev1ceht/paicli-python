from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SENSITIVE_KEYS = ("token", "key", "password", "secret", "authorization", "bearer")


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def record(
        self,
        *,
        tool_name: str,
        input_data: dict[str, Any],
        outcome: str,
        approver: str,
        cwd: str,
        result_summary: str | None = None,
        decision_source: str | None = None,
        reason: str | None = None,
    ) -> None:
        timestamp = datetime.now(UTC)
        target = self._path_for_timestamp(timestamp)
        target.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "timestamp": timestamp.isoformat(),
            "tool_name": tool_name,
            "input": self._redact(input_data),
            "outcome": outcome,
            "approver": approver,
            "cwd": cwd,
        }
        if result_summary is not None:
            event["result_summary"] = self._redact(result_summary)[:2000]
        if decision_source:
            event["decision_source"] = decision_source
        if reason:
            event["reason"] = reason
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def ensure_available(self) -> None:
        target = self._path_for_timestamp(datetime.now(UTC))
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8"):
            pass

    def record_retry(
        self,
        *,
        scope: str,
        operation: str,
        logical_call_id: str,
        attempt: int,
        error_kind: str,
        retry_delay: float,
        outcome: str = "scheduled",
        cwd: str = "",
        input_data: dict[str, Any] | None = None,
    ) -> None:
        timestamp = datetime.now(UTC)
        target = self._path_for_timestamp(timestamp)
        target.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "timestamp": timestamp.isoformat(),
            "event_type": "retry",
            "scope": scope,
            "operation": operation,
            "logical_call_id": logical_call_id,
            "attempt": attempt,
            "error_kind": error_kind,
            "retry_delay": retry_delay,
            "outcome": outcome,
            "cwd": cwd,
        }
        if input_data is not None:
            event["input"] = self._redact(input_data)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def tail(self, limit: int = 20) -> list[dict[str, Any]]:
        paths = self._log_files()
        if not paths:
            return []
        events = []
        for path in paths:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events[-limit:]

    def _path_for_timestamp(self, timestamp: datetime) -> Path:
        if self.path.suffix == ".jsonl":
            return self.path
        return self.path / f"audit-{timestamp.date().isoformat()}.jsonl"

    def _log_files(self) -> list[Path]:
        if self.path.is_file():
            return [self.path]
        if not self.path.exists():
            return []
        return sorted(self.path.glob("*.jsonl"))

    def _redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            redacted = {}
            for key, item in value.items():
                if any(marker in key.lower() for marker in SENSITIVE_KEYS):
                    redacted[key] = "***"
                else:
                    redacted[key] = self._redact(item)
            return redacted
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value
