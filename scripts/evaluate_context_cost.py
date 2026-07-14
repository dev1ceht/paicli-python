"""Run PaiCLI's scripted context-cost evaluation without changing normal Agent paths."""

from __future__ import annotations

import argparse
from pathlib import Path

from paicli.evaluation.context_cost import run_scripted_context_cost


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run scripted PaiCLI context-cost evaluation.")
    parser.add_argument(
        "--tasks",
        type=Path,
        default=Path("benchmarks/long_session_tasks.json"),
        help="Path to the scripted task manifest.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/context-cost"),
        help="Directory for isolated workspaces, traces, and reports.",
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        default=2,
        help="Runs per task and variant; two verifies deterministic replay.",
    )
    args = parser.parse_args(argv)
    payload = run_scripted_context_cost(
        args.tasks,
        output_dir=args.output_dir,
        repetitions=args.repetitions,
    )
    print(f"results: {args.output_dir / 'results.json'}")
    print(f"report: {args.output_dir / 'report.md'}")
    return 0 if payload["determinism"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
