from __future__ import annotations

import json
import re

from paicli.policy import AuditLog


def test_audit_log_writes_daily_jsonl_file(tmp_path):
    audit_dir = tmp_path / "audit"
    audit = AuditLog(audit_dir)

    audit.record(
        tool_name="write",
        input_data={"path": "note.txt", "api_key": "secret"},
        outcome="allow",
        approver="hitl",
        cwd=str(tmp_path),
    )

    files = list(audit_dir.glob("*.jsonl"))
    assert len(files) == 1
    assert files[0].name.startswith("audit-")
    assert files[0].name.endswith(".jsonl")
    assert files[0].name.count("-") == 3
    event = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert event["tool_name"] == "write"
    assert event["input"]["api_key"] == "***"
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
        event["timestamp"],
    )


def test_audit_log_tail_reads_across_daily_files(tmp_path):
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    (audit_dir / "audit-2026-07-08.jsonl").write_text(
        json.dumps({"timestamp": "2026-07-08T10:00:00+00:00", "tool_name": "old"})
        + "\n",
        encoding="utf-8",
    )
    (audit_dir / "audit-2026-07-09.jsonl").write_text(
        json.dumps({"timestamp": "2026-07-09T10:00:00+00:00", "tool_name": "new"})
        + "\n",
        encoding="utf-8",
    )

    events = AuditLog(audit_dir).tail(2)

    assert [event["tool_name"] for event in events] == ["old", "new"]
    assert [event["timestamp"] for event in events] == [
        "2026-07-08 18:00:00",
        "2026-07-09 18:00:00",
    ]


def test_audit_log_tail_orders_mixed_legacy_and_current_timestamps(tmp_path):
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    (audit_dir / "audit-2026-07-08.jsonl").write_text(
        json.dumps(
            {
                "timestamp": "2026-07-08T23:30:00+00:00",
                "tool_name": "actually-latest",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (audit_dir / "audit-2026-07-09.jsonl").write_text(
        json.dumps(
            {
                "timestamp": "2026-07-09 00:30:00",
                "tool_name": "earlier-current",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    events = AuditLog(audit_dir).tail(1)

    assert [event["tool_name"] for event in events] == ["actually-latest"]
    assert [event["timestamp"] for event in events] == ["2026-07-09 07:30:00"]
