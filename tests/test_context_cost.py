from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


def test_scripted_context_cost_runner_executes_isolated_fixture_and_writes_artifacts(tmp_path):
    from paicli.evaluation.context_cost import run_scripted_context_cost

    fixture = tmp_path / "fixtures" / "rename"
    fixture.mkdir(parents=True)
    (fixture / "message.txt").write_text("before\n", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "tasks": [
            {
                "id": "rename-message",
                "prompt": "Replace the message text and verify it.",
                "fixture_repo": "fixtures/rename",
                "allowed_tools": ["read_file", "write_file"],
                "step_budget": 4,
                "scripted_outputs": [
                    {"type": "tool", "name": "read_file", "arguments": {"path": "message.txt"}},
                    {
                        "type": "tool",
                        "name": "write_file",
                        "arguments": {"path": "message.txt", "content": "after\n"},
                    },
                    {"type": "final", "text": "Updated and verified message.txt."},
                ],
                "llm_handoff": {
                    "summary": (
                        "## Goal\nUpdate the fixture.\n\n## Next Steps\nFinish verification."
                    ),
                    "usage": {"input_tokens": 11, "output_tokens": 7},
                },
                "verifier": [
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; "
                        "assert Path('message.txt').read_text() == 'after\\n'"
                    ),
                ],
            }
        ],
    }
    manifest_path = tmp_path / "long_session_tasks.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    output_dir = tmp_path / "artifacts"

    payload = run_scripted_context_cost(manifest_path, output_dir=output_dir, repetitions=2)

    assert len(payload["rows"]) == 6
    assert {row["variant"] for row in payload["rows"]} == {
        "no_context_reduction",
        "full_orchestrator",
        "full_orchestrator_with_llm_handoff",
    }
    assert all(row["status"] == "passed" for row in payload["rows"])
    assert all(row["usage_source"] == "estimated_proxy" for row in payload["rows"])
    assert all(Path(row["trace_path"]).is_file() for row in payload["rows"])
    assert (output_dir / "results.json").is_file()
    assert (output_dir / "report.md").is_file()
    assert (fixture / "message.txt").read_text(encoding="utf-8") == "before\n"
    assert payload["determinism"]["passed"] is True
    by_variant = {row["variant"]: row for row in payload["rows"] if row["repeat"] == 0}
    assert by_variant["full_orchestrator_with_llm_handoff"]["summary_called"] is True
    assert by_variant["full_orchestrator_with_llm_handoff"]["compact_call_total_tokens"] == 18
    assert (
        by_variant["full_orchestrator"]["input_tokens"]
        < by_variant["no_context_reduction"]["input_tokens"]
    )
    assert (
        payload["summary"]["comparisons"]["full_history_vs_deterministic"]["paired_run_count"] == 2
    )
    baseline_trace = Path(by_variant["no_context_reduction"]["trace_path"]).read_text(
        encoding="utf-8"
    )
    handoff_trace = Path(by_variant["full_orchestrator_with_llm_handoff"]["trace_path"]).read_text(
        encoding="utf-8"
    )
    assert '"pressure_tier": "disabled"' in baseline_trace
    assert '"summary_mode": "llm"' in handoff_trace


def test_scripted_context_cost_runner_marks_a_failed_verifier_as_failed(tmp_path):
    from paicli.evaluation.context_cost import run_scripted_context_cost

    fixture = tmp_path / "fixtures" / "broken"
    fixture.mkdir(parents=True)
    manifest = {
        "schema_version": 1,
        "tasks": [
            {
                "id": "broken-verifier",
                "prompt": "Finish the fixed scripted task.",
                "fixture_repo": "fixtures/broken",
                "allowed_tools": [],
                "step_budget": 1,
                "scripted_outputs": [{"type": "final", "text": "done"}],
                "llm_handoff": {
                    "summary": "## Goal\nFinish.\n\n## Next Steps\nVerify.",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
                "verifier": [sys.executable, "-c", "raise SystemExit(1)"],
            }
        ],
    }
    manifest_path = tmp_path / "long_session_tasks.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    payload = run_scripted_context_cost(
        manifest_path, output_dir=tmp_path / "artifacts", repetitions=2
    )

    assert {row["status"] for row in payload["rows"]} == {"failed"}


def test_scripted_context_cost_runner_requires_two_repetitions(tmp_path):
    from paicli.evaluation.context_cost import run_scripted_context_cost

    manifest_path = tmp_path / "long_session_tasks.json"
    manifest_path.write_text(json.dumps({"tasks": [{"id": "unused"}]}), encoding="utf-8")

    with pytest.raises(ValueError, match="at least two repetitions"):
        run_scripted_context_cost(manifest_path, output_dir=tmp_path / "artifacts", repetitions=1)


def test_project_context_cost_manifest_contains_five_isolated_native_tool_tasks():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads(
        (root / "benchmarks" / "long_session_tasks.json").read_text(encoding="utf-8")
    )

    tasks = manifest["tasks"]
    assert len(tasks) == 5
    for task in tasks:
        assert (root / "benchmarks" / task["fixture_repo"]).is_dir()
        assert task["verifier"]
        assert all(action["type"] in {"tool", "final"} for action in task["scripted_outputs"])
        assert all(
            action.get("name") in {"read_file", "write_file"}
            for action in task["scripted_outputs"]
            if action["type"] == "tool"
        )
