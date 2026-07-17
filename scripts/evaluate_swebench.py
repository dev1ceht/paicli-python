"""Prepare, generate, and report PaiCLI SWE-bench Lite A/B evaluations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Never

from paicli.evaluation.swebench import (
    compare_swebench_experiment,
    fetch_swebench_dataset,
    freeze_swebench_selection_manifests,
    import_swebench_dataset,
    import_swebench_harness_results,
    load_context_stress_profile,
    load_swebench_selection,
    prepare_swebench_repositories,
    run_swebench_generation,
)


class _EvaluationArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError(message)


def _build_parser() -> argparse.ArgumentParser:
    parser = _EvaluationArgumentParser(
        description="PaiCLI SWE-bench Lite prediction generation and result reporting."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    fetch = commands.add_parser("fetch-dataset", help="Fetch a pinned official Lite snapshot.")
    fetch.add_argument("--revision", required=True, help="Exact Hugging Face dataset revision.")
    fetch.add_argument(
        "--allow-network",
        action="store_true",
        help="Explicitly authorize downloading the pinned dataset snapshot.",
    )
    fetch.add_argument("--output-root", type=Path, default=Path("artifacts/swebench-lite/datasets"))
    _add_selection_manifest_argument(fetch)

    local_import = commands.add_parser(
        "import-dataset", help="Import an existing local official-format JSON snapshot."
    )
    local_import.add_argument("--source", required=True, type=Path)
    local_import.add_argument(
        "--output-root", type=Path, default=Path("artifacts/swebench-lite/datasets")
    )
    _add_selection_manifest_argument(local_import)

    prepare = commands.add_parser("prepare", help="Prepare reusable bare Git mirrors.")
    _add_snapshot_selection_arguments(prepare)
    prepare.add_argument(
        "--cache-root", type=Path, default=Path("artifacts/swebench-lite/repo-cache")
    )
    prepare.add_argument(
        "--allow-network",
        action="store_true",
        help="Allow cloning missing mirrors and fetching existing mirrors.",
    )

    generate = commands.add_parser("generate", help="Generate both A/B prediction files serially.")
    _add_snapshot_selection_arguments(generate)
    generate.add_argument(
        "--cache-root", type=Path, default=Path("artifacts/swebench-lite/repo-cache")
    )
    generate.add_argument("--output-dir", type=Path, required=True)
    generate.add_argument(
        "--context-profile",
        type=Path,
        default=Path("benchmarks/swebench-lite-v1/profiles/stress-32k-v1.json"),
    )
    generate.add_argument("--development", action="store_true")
    generate.add_argument("--keep-workspaces", action="store_true")

    report = commands.add_parser("report", help="Import one official harness result set.")
    report.add_argument("--experiment-dir", type=Path, required=True)
    report.add_argument("--variant", choices=("full-history", "optimized"), required=True)
    report.add_argument("--harness-results-dir", type=Path, required=True)
    report.add_argument("--harness-revision", required=True)
    report.add_argument("--development", action="store_true")

    compare = commands.add_parser("compare", help="Create the paired A/B comparison report.")
    compare.add_argument("--experiment-dir", type=Path, required=True)
    return parser


def _add_snapshot_selection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument(
        "--selection",
        choices=(
            "capability-30",
            "context-stress-10",
            "context-stress-5-v1",
            "flask-pilot-1-v1",
        ),
        default="context-stress-5-v1",
    )


def _add_selection_manifest_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--selection-manifest-root",
        type=Path,
        default=Path("benchmarks/swebench-lite-v1/selections"),
        help="Directory for the reviewable fixed ordered-ID manifests.",
    )


def main(argv: list[str] | None = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
    except ValueError as exc:
        print(f"argument error: {exc}", file=sys.stderr)
        return 1
    try:
        result = _dispatch(args)
    except KeyboardInterrupt:
        print("SWE-bench stage interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - stage boundary reports invalid pipelines
        print(f"SWE-bench stage failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def _dispatch(args: argparse.Namespace) -> object:
    if args.command == "fetch-dataset":
        if not args.allow_network:
            raise ValueError("fetch-dataset requires explicit --allow-network authorization")
        result = fetch_swebench_dataset(output_root=args.output_root, revision=args.revision)
        freeze_swebench_selection_manifests(result, manifest_root=args.selection_manifest_root)
        return result
    if args.command == "import-dataset":
        result = import_swebench_dataset(args.source, output_root=args.output_root)
        freeze_swebench_selection_manifests(result, manifest_root=args.selection_manifest_root)
        return result
    if args.command == "prepare":
        instances = load_swebench_selection(args.snapshot_dir, selection=args.selection)
        prepared = prepare_swebench_repositories(
            instances,
            cache_root=args.cache_root,
            allow_network=args.allow_network,
        )
        return {
            "prepared": len(prepared),
            "cache_root": str(args.cache_root.resolve()),
            "instance_ids": [item.instance_id for item in prepared],
        }
    if args.command == "generate":
        if args.selection == "flask-pilot-1-v1" and not args.development:
            raise ValueError("flask-pilot-1-v1 requires --development")
        instances = load_swebench_selection(args.snapshot_dir, selection=args.selection)
        profile = load_context_stress_profile(args.context_profile)
        metadata = json.loads(
            (args.snapshot_dir.resolve() / "metadata.json").read_text(encoding="utf-8")
        )
        return run_swebench_generation(
            instances,
            cache_root=args.cache_root,
            output_dir=args.output_dir,
            context_profile=profile,
            dataset_identity={
                "dataset_fingerprint": str(metadata["dataset_fingerprint"]),
                "selection_id": args.selection,
                "selection_fingerprint": _selection_fingerprint(
                    args.snapshot_dir,
                    args.selection,
                    metadata,
                ),
                "snapshot_dir": str(args.snapshot_dir.resolve()),
            },
            formal=not args.development,
            keep_workspaces=args.keep_workspaces,
        )
    if args.command == "report":
        return import_swebench_harness_results(
            args.experiment_dir,
            variant=args.variant,
            harness_results_dir=args.harness_results_dir,
            harness_revision=args.harness_revision,
            formal=not args.development,
        )
    if args.command == "compare":
        return compare_swebench_experiment(args.experiment_dir)
    raise ValueError(f"unsupported SWE-bench command: {args.command}")


def _selection_fingerprint(
    snapshot_dir: Path,
    selection: str,
    metadata: dict[str, object],
) -> str:
    fingerprints = metadata.get("selection_fingerprints")
    if isinstance(fingerprints, dict) and selection in fingerprints:
        return str(fingerprints[selection])
    manifest = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "benchmarks"
            / "swebench-lite-v1"
            / "selections"
            / f"{selection}.json"
        ).read_text(encoding="utf-8")
    )
    if manifest.get("dataset_fingerprint") != metadata.get("dataset_fingerprint"):
        raise ValueError(f"selection manifest does not match snapshot: {snapshot_dir}")
    return str(manifest["selection_fingerprint"])


if __name__ == "__main__":
    raise SystemExit(main())
