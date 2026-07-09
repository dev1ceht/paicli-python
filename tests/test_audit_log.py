from __future__ import annotations

import json

from paicli.policy import AuditLog


def test_audit_log_writes_daily_jsonl_file(tmp_path):
    audit_dir = tmp_path / "audit"
    audit = AuditLog(audit_dir)

    audit.record(
        tool_name="write_file",
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
    assert event["tool_name"] == "write_file"
    assert event["input"]["api_key"] == "***"


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
