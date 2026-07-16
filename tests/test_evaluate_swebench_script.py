from __future__ import annotations

import subprocess
import sys


def test_swebench_script_exposes_only_the_six_stage_commands() -> None:
    process = subprocess.run(
        [sys.executable, "scripts/evaluate_swebench.py", "--help"],
        check=False,
        text=True,
        capture_output=True,
    )

    assert process.returncode == 0
    for command in ("fetch-dataset", "import-dataset", "prepare", "generate", "report", "compare"):
        assert command in process.stdout
    assert "score" not in process.stdout


def test_swebench_script_rejects_max_workers() -> None:
    process = subprocess.run(
        [sys.executable, "scripts/evaluate_swebench.py", "generate", "--max-workers", "2"],
        check=False,
        text=True,
        capture_output=True,
    )

    assert process.returncode == 1
    assert "argument error" in process.stderr
