"""Run PaiCLI's local coding smoke benchmark through the production Agent path."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Never

from paicli.evaluation.local_smoke import local_smoke_exit_code, run_local_smoke


class _BenchmarkArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError(message)


def main(argv: list[str] | None = None) -> int:
    parser = _BenchmarkArgumentParser(description="Run PaiCLI's local coding smoke benchmark.")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("benchmarks/local-smoke-v1/tasks.json"),
        help="Path to the strict local-smoke suite manifest.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/local-smoke-v1"),
        help="Directory for results, reports, per-attempt artifacts, and temporary runs.",
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        default=1,
        help="Fresh serial attempts per task; use three for formal comparisons.",
    )
    parser.add_argument(
        "--allow-unsandboxed",
        action="store_true",
        help="Acknowledge that live Agent tools have filesystem and network access.",
    )
    parser.add_argument(
        "--require-clean-runtime",
        action="store_true",
        help="Reject execution when the PaiCLI source worktree is dirty.",
    )
    parser.add_argument(
        "--keep-workspaces",
        action="store_true",
        help="Retain per-attempt workspaces for debugging.",
    )
    parser.add_argument(
        "--compare-contexts",
        action="store_true",
        help="Run counterbalanced full-history and optimized context variants.",
    )
    parser.add_argument(
        "--context-profile",
        type=Path,
        help="Path to the fixed context-budget profile used by a context comparison.",
    )
    try:
        args = parser.parse_args(argv)
    except ValueError as exc:
        print(f"argument error: {exc}", file=sys.stderr)
        return 1

    try:
        payload = run_local_smoke(
            args.manifest,
            output_dir=args.output_dir,
            repetitions=args.repetitions,
            allow_unsandboxed=args.allow_unsandboxed,
            require_clean_runtime=args.require_clean_runtime,
            keep_workspaces=args.keep_workspaces,
            compare_contexts=args.compare_contexts,
            context_profile=args.context_profile,
        )
    except KeyboardInterrupt:
        print("benchmark interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - command boundary reports setup failures
        print(f"benchmark setup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"results: {args.output_dir / 'results.json'}")
    print(f"report: {args.output_dir / 'report.md'}")
    return local_smoke_exit_code(payload)


if __name__ == "__main__":
    raise SystemExit(main())
