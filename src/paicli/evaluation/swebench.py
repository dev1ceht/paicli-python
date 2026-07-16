"""SWE-bench Lite prediction generation and result reporting."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from paicli.config import PaiCliConfig
from paicli.context import ContextBuildResult, ContextManager, ContextWindowExceededError
from paicli.context.pressure import calculate_pressure_from_tokens
from paicli.llm import create_llm_client
from paicli.llm.base import LlmClient
from paicli.prompt import PromptSections
from paicli.tools import ToolRegistry, get_builtin_tools
from paicli.types import Message

_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GENERATION_VARIANTS = ("full-history", "optimized")
_CAPABILITY_SEED = "paicli-capability-30-v1"
_STRESS_SEED = "paicli-context-stress-10-v1"
_TOOL_PROFILE = frozenset(
    {
        "bash",
        "execute_command",
        "glob",
        "glob_files",
        "grep",
        "grep_code",
        "list_dir",
        "read_file",
        "search_code",
        "write_file",
    }
)


@dataclass(frozen=True, slots=True)
class SweBenchInstance:
    """The generation-safe projection of one SWE-bench source record."""

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str


@dataclass(frozen=True, slots=True)
class ContextStressProfile:
    """A named immutable per-request context budget."""

    profile_id: str
    input_budget_tokens: int
    output_reserve_tokens: int
    fingerprint: str


@dataclass(frozen=True, slots=True)
class PreparedRepository:
    instance_id: str
    repo: str
    base_commit: str
    mirror_path: Path


@dataclass(frozen=True, slots=True)
class SweBenchWorkspace:
    path: Path
    base_commit: str


class FullHistoryContextManager(ContextManager):
    """Benchmark baseline that preserves history under a fixed input guard."""

    def __init__(
        self,
        *,
        config: PaiCliConfig,
        llm_client: LlmClient,
        cwd: str,
        input_budget_tokens: int,
    ) -> None:
        super().__init__(config=config, llm_client=llm_client, cwd=cwd)
        self.input_budget_tokens = input_budget_tokens

    async def build_turn_context(
        self,
        *,
        prefix: str = "",
        memory: str = "",
        skills: str = "",
        relevant_memory: str = "",
        messages: list[Message] | None = None,
        prompt_sections: PromptSections | None = None,
        tools: list[dict[str, Any]] | None = None,
        actual_usage: dict[str, int] | None = None,
    ) -> ContextBuildResult:
        del actual_usage
        sections = prompt_sections or PromptSections(
            prefix="\n\n".join(part for part in (prefix, memory) if part),
            relevant_memory=relevant_memory,
            skills=skills,
        )
        output_messages = list(messages or [])
        prepared = self.llm_client.prepare_request(
            output_messages,
            list(tools or []),
            system_prompt=sections.render(),
        ).with_quality_budget(self.input_budget_tokens, self.pressure_thresholds())
        pressure = calculate_pressure_from_tokens(
            prepared.estimated_input_tokens,
            self.input_budget_tokens,
            self.config.context,
        )
        if prepared.estimated_input_tokens > self.input_budget_tokens:
            raise ContextWindowExceededError(
                "The full-history request exceeds the benchmark input budget "
                f"({prepared.estimated_input_tokens} > {self.input_budget_tokens} input tokens)."
            )
        self._last_pressure = pressure
        return ContextBuildResult(
            system_prompt=sections.render(),
            messages=output_messages,
            prepared=prepared,
            pressure_before=pressure,
            pressure_after=pressure,
            reductions=[],
            compacted=False,
            pressure_tier=pressure.tier.value,
        )

    def quality_budget_tokens(self) -> int:
        return self.input_budget_tokens


def full_history_context_manager_factory(
    profile: ContextStressProfile,
) -> Callable[..., ContextManager]:
    """Return the production Agent construction seam for the baseline variant."""

    def factory(*, config: PaiCliConfig, llm_client: LlmClient, cwd: str) -> ContextManager:
        return FullHistoryContextManager(
            config=config,
            llm_client=llm_client,
            cwd=cwd,
            input_budget_tokens=profile.input_budget_tokens,
        )

    return factory


def load_swebench_instances(path: str | Path) -> tuple[SweBenchInstance, ...]:
    """Load JSON or JSONL records and expose only Agent generation fields."""

    source = Path(path).resolve()
    text = source.read_text(encoding="utf-8")
    records = _decode_records(text)
    instances: list[SweBenchInstance] = []
    seen: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"SWE-bench record {index} must be an object")
        instance = SweBenchInstance(
            instance_id=_required_text(record, "instance_id", index),
            repo=_required_text(record, "repo", index),
            base_commit=_required_text(record, "base_commit", index),
            problem_statement=_required_text(record, "problem_statement", index),
        )
        if instance.instance_id in seen:
            raise ValueError(f"duplicate SWE-bench instance_id: {instance.instance_id}")
        if not _REPOSITORY_PATTERN.fullmatch(instance.repo):
            raise ValueError(f"invalid SWE-bench repository identity: {instance.repo}")
        seen.add(instance.instance_id)
        instances.append(instance)
    if not instances:
        raise ValueError("SWE-bench source contains no instances")
    return tuple(instances)


def import_swebench_dataset(
    source_path: str | Path,
    *,
    output_root: str | Path,
    capability_count: int = 30,
    stress_count: int = 10,
    source_kind: str = "local-import",
    source_revision: str | None = None,
) -> dict[str, Any]:
    """Create an immutable full-source snapshot plus deterministic ID selections."""

    source = Path(source_path).resolve()
    raw_records = _decode_records(source.read_text(encoding="utf-8"))
    if any(not isinstance(record, dict) for record in raw_records):
        raise ValueError("SWE-bench dataset records must be objects")
    instances = load_swebench_instances(source)
    if stress_count > capability_count:
        raise ValueError("context-stress selection cannot exceed capability selection")
    capability = select_repository_balanced_instances(
        instances,
        count=capability_count,
        seed=_CAPABILITY_SEED,
    )
    stress = select_repository_balanced_instances(
        capability,
        count=stress_count,
        seed=_STRESS_SEED,
    )
    canonical = json.dumps(
        raw_records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    fingerprint = hashlib.sha256(canonical).hexdigest()
    target = Path(output_root).resolve() / fingerprint
    metadata = {
        "artifact_schema_version": 1,
        "dataset_fingerprint": fingerprint,
        "record_count": len(instances),
        "source": {
            "kind": source_kind,
            "path": str(source) if source_kind == "local-import" else None,
            "revision": source_revision,
        },
        "reference_data_confidentiality": False,
        "selections": {
            "capability-30": [item.instance_id for item in capability],
            "context-stress-10": [item.instance_id for item in stress],
        },
        "selection_fingerprints": {
            "capability-30": _hash_json([item.instance_id for item in capability]),
            "context-stress-10": _hash_json([item.instance_id for item in stress]),
        },
        "selection_seeds": {
            "capability-30": _CAPABILITY_SEED,
            "context-stress-10": _STRESS_SEED,
        },
    }
    if target.exists():
        existing = _read_json_object(target / "metadata.json")
        if existing != metadata:
            raise ValueError(
                "immutable dataset snapshot already exists with different source or selections: "
                f"{target}"
            )
    else:
        target.mkdir(parents=True)
        _atomic_write_text(
            target / "dataset.json",
            json.dumps(raw_records, ensure_ascii=False, indent=2) + "\n",
        )
        _atomic_write_json(target / "metadata.json", metadata)
    return {**metadata, "snapshot_dir": str(target)}


def fetch_swebench_dataset(
    *,
    output_root: str | Path,
    revision: str,
    capability_count: int = 30,
    stress_count: int = 10,
) -> dict[str, Any]:
    """Fetch the official Lite test split through the optional datasets extra."""

    if not revision.strip():
        raise ValueError("fetch-dataset requires a pinned dataset revision")
    try:
        from datasets import load_dataset  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "fetch-dataset requires the optional dependency: pip install -e .[swebench]"
        ) from exc
    dataset = load_dataset(
        "SWE-bench/SWE-bench_Lite",
        split="test",
        revision=revision,
    )
    records = [dict(record) for record in dataset]
    with tempfile.TemporaryDirectory(prefix="paicli-swebench-dataset-") as temporary:
        source = Path(temporary) / "dataset.json"
        source.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        return import_swebench_dataset(
            source,
            output_root=output_root,
            capability_count=capability_count,
            stress_count=stress_count,
            source_kind="huggingface",
            source_revision=revision,
        )


def load_swebench_selection(
    snapshot_dir: str | Path,
    *,
    selection: str = "context-stress-10",
) -> tuple[SweBenchInstance, ...]:
    """Load a frozen selection while returning only generation-safe fields."""

    root = Path(snapshot_dir).resolve()
    metadata = _read_json_object(root / "metadata.json")
    selections = metadata.get("selections")
    if not isinstance(selections, dict) or selection not in selections:
        raise ValueError(f"unknown SWE-bench snapshot selection: {selection}")
    selected_value = selections[selection]
    selected_ids = _instance_id_set(selected_value, f"selection {selection}")
    instances = load_swebench_instances(root / "dataset.json")
    indexed = {item.instance_id: item for item in instances}
    missing = selected_ids - indexed.keys()
    if missing:
        raise ValueError(f"snapshot selection references missing instances: {sorted(missing)}")
    return tuple(indexed[instance_id] for instance_id in selected_value)


def freeze_swebench_selection_manifests(
    snapshot: dict[str, Any],
    *,
    manifest_root: str | Path,
) -> tuple[Path, ...]:
    """Write reviewable fixed-suite manifests that formal clean runs must match."""

    target = Path(manifest_root).resolve()
    source = snapshot.get("source")
    selections = snapshot.get("selections")
    fingerprints = snapshot.get("selection_fingerprints")
    if (
        not isinstance(source, dict)
        or not isinstance(selections, dict)
        or not isinstance(fingerprints, dict)
    ):
        raise ValueError("dataset snapshot is missing selection identity fields")
    written: list[Path] = []
    for selection_id in ("capability-30", "context-stress-10"):
        payload = {
            "artifact_schema_version": 1,
            "selection_id": selection_id,
            "dataset_fingerprint": snapshot["dataset_fingerprint"],
            "source": source,
            "instance_ids": selections[selection_id],
            "selection_fingerprint": fingerprints[selection_id],
        }
        path = target / f"{selection_id}.json"
        if path.exists() and _read_json_object(path) != payload:
            raise ValueError(f"fixed selection manifest already exists with different IDs: {path}")
        _atomic_write_json(path, payload)
        written.append(path)
    return tuple(written)


def select_repository_balanced_instances(
    instances: tuple[SweBenchInstance, ...],
    *,
    count: int,
    seed: str,
) -> tuple[SweBenchInstance, ...]:
    """Select a stable repository-round subset without model-derived signals."""

    if count < 1 or count > len(instances):
        raise ValueError("selection count must be within the available instance population")
    if not seed:
        raise ValueError("selection seed must not be empty")
    grouped: dict[str, list[SweBenchInstance]] = defaultdict(list)
    for instance in instances:
        grouped[instance.repo].append(instance)
    for group in grouped.values():
        group.sort(key=lambda item: _selection_digest(seed, item.instance_id))

    selected: list[SweBenchInstance] = []
    round_index = 0
    repositories = sorted(grouped, key=lambda repo: _selection_digest(seed, repo))
    while len(selected) < count:
        for repository in repositories:
            group = grouped[repository]
            if round_index < len(group):
                selected.append(group[round_index])
                if len(selected) == count:
                    return tuple(selected)
        round_index += 1
    return tuple(selected)


def load_context_stress_profile(path: str | Path) -> ContextStressProfile:
    """Load and fingerprint a strict named context-stress profile."""

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("context-stress profile must be an object")
    expected = {
        "schema_version",
        "profile_id",
        "input_budget_tokens",
        "output_reserve_tokens",
    }
    if set(data) != expected or data.get("schema_version") != 1:
        raise ValueError("invalid context-stress profile schema")
    profile_id = data.get("profile_id")
    input_budget = data.get("input_budget_tokens")
    output_reserve = data.get("output_reserve_tokens")
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ValueError("context-stress profile_id must not be empty")
    if not isinstance(input_budget, int) or isinstance(input_budget, bool) or input_budget < 1:
        raise ValueError("input_budget_tokens must be a positive integer")
    if (
        not isinstance(output_reserve, int)
        or isinstance(output_reserve, bool)
        or output_reserve < 1
    ):
        raise ValueError("output_reserve_tokens must be a positive integer")
    fingerprint = hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ContextStressProfile(
        profile_id=profile_id,
        input_budget_tokens=input_budget,
        output_reserve_tokens=output_reserve,
        fingerprint=fingerprint,
    )


def prepare_swebench_repositories(
    instances: tuple[SweBenchInstance, ...],
    *,
    cache_root: str | Path,
    allow_network: bool = False,
) -> tuple[PreparedRepository, ...]:
    """Ensure a bare mirror contains every selected instance base commit."""

    root = Path(cache_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    prepared: list[PreparedRepository] = []
    refreshed: set[str] = set()
    for instance in instances:
        mirror = root / f"{instance.repo.replace('/', '__')}.git"
        if not mirror.exists():
            if not allow_network:
                raise FileNotFoundError(f"missing repository cache: {mirror}")
            _run_git(
                None,
                "clone",
                "--mirror",
                f"https://github.com/{instance.repo}.git",
                str(mirror),
            )
        if instance.repo not in refreshed and allow_network:
            _run_git(mirror, "fetch", "--prune")
            refreshed.add(instance.repo)
        commit = _run_git(
            mirror,
            "rev-parse",
            "--verify",
            f"{instance.base_commit}^{{commit}}",
        ).stdout.strip()
        prepared.append(
            PreparedRepository(
                instance_id=instance.instance_id,
                repo=instance.repo,
                base_commit=commit,
                mirror_path=mirror,
            )
        )
    return tuple(prepared)


def materialize_swebench_workspace(
    instance: SweBenchInstance,
    *,
    cache_root: str | Path,
    destination: str | Path,
) -> SweBenchWorkspace:
    """Create one independent clean detached clone for an Agent attempt."""

    root = Path(cache_root).resolve()
    mirror = root / f"{instance.repo.replace('/', '__')}.git"
    target = Path(destination).resolve()
    if target.exists():
        raise FileExistsError(f"SWE-bench workspace already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    _run_git(None, "clone", "--no-checkout", "--quiet", str(mirror), str(target))
    _run_git(target, "checkout", "--detach", "--quiet", instance.base_commit)
    actual = _run_git(target, "rev-parse", "HEAD").stdout.strip()
    if actual != instance.base_commit:
        raise RuntimeError(
            f"SWE-bench workspace HEAD mismatch for {instance.instance_id}: "
            f"{actual} != {instance.base_commit}"
        )
    if _run_git(target, "status", "--porcelain").stdout.strip():
        raise RuntimeError(f"SWE-bench workspace is dirty before generation: {target}")
    return SweBenchWorkspace(path=target, base_commit=actual)


def run_swebench_generation(
    instances: tuple[SweBenchInstance, ...],
    *,
    cache_root: str | Path,
    output_dir: str | Path,
    context_profile: ContextStressProfile,
    client_factory: Callable[[SweBenchInstance, str, PaiCliConfig], Any] | None = None,
    dataset_identity: dict[str, str] | None = None,
    formal: bool = True,
    keep_workspaces: bool = False,
) -> dict[str, Any]:
    """Generate one immutable serial A/B experiment through the production Agent."""

    if not instances:
        raise ValueError("SWE-bench generation requires at least one instance")
    if formal and client_factory is not None:
        raise ValueError("formal generation does not accept a custom model client factory")
    if formal and keep_workspaces:
        raise ValueError("formal generation cannot retain Agent workspaces")
    target = Path(output_dir).resolve()
    runtime = _runtime_identity()
    if formal and runtime["dirty"]:
        raise ValueError("formal SWE-bench generation requires a clean PaiCLI runtime")
    if formal and (
        not dataset_identity
        or not dataset_identity.get("dataset_fingerprint")
        or not dataset_identity.get("selection_id")
    ):
        raise ValueError("formal generation requires dataset fingerprint and selection identity")
    if formal:
        assert dataset_identity is not None
        _validate_formal_generation_identity(instances, context_profile, dataset_identity)
    base_config = _generation_config(context_profile, target)
    if formal and (base_config.llm.provider != "qwen" or base_config.llm.model != "qwen3.6-flash"):
        raise ValueError("swebench-lite-v1 formal runs require qwen/qwen3.6-flash")
    factory = client_factory or _live_generation_client
    expected: dict[str, Any] = {
        "artifact_schema_version": 1,
        "formal": formal,
        "runtime_identity": runtime,
        "dataset_identity": dataset_identity
        or {
            "dataset_fingerprint": "development-unknown",
            "selection_id": "development-unknown",
        },
        "environment_identity": _environment_identity(),
        "context_profile": {
            "profile_id": context_profile.profile_id,
            "input_budget_tokens": context_profile.input_budget_tokens,
            "output_reserve_tokens": context_profile.output_reserve_tokens,
            "fingerprint": context_profile.fingerprint,
        },
        "configuration_identity": _generation_configuration_identity(base_config),
        "instance_ids": [instance.instance_id for instance in instances],
        "problem_statement_sha256": {
            instance.instance_id: hashlib.sha256(instance.problem_statement.encode()).hexdigest()
            for instance in instances
        },
        "attempts": [],
    }
    if target.exists():
        payload = _load_resumable_experiment(target, expected)
    else:
        target.mkdir(parents=True)
        payload = expected
        _atomic_write_json(target / "experiment.json", payload)
    attempts = payload["attempts"]
    completed_keys = {(item["variant"], item["instance_id"]) for item in attempts}

    for task_index, instance in enumerate(instances):
        order = (
            _GENERATION_VARIANTS if task_index % 2 == 0 else tuple(reversed(_GENERATION_VARIANTS))
        )
        for variant in order:
            if (variant, instance.instance_id) in completed_keys:
                continue
            attempt = _run_generation_attempt(
                instance,
                variant=variant,
                cache_root=Path(cache_root).resolve(),
                experiment_dir=target,
                base_config=base_config,
                context_profile=context_profile,
                client_factory=factory,
                usage_source="synthetic" if client_factory is not None else "provider_reported",
                keep_workspace=keep_workspaces,
            )
            attempts.append(attempt)
            completed_keys.add((variant, instance.instance_id))
            payload["attempts"] = attempts
            _atomic_write_json(target / "experiment.json", payload)

    attempt_index = {(item["variant"], item["instance_id"]): item for item in attempts}
    predictions: dict[str, list[dict[str, str]]] = {
        variant: [
            {
                "instance_id": instance.instance_id,
                "model_name_or_path": str(attempt_index[(variant, instance.instance_id)]["model"]),
                "model_patch": _load_attempt_patch(
                    target, attempt_index[(variant, instance.instance_id)]
                ),
            }
            for instance in instances
        ]
        for variant in _GENERATION_VARIANTS
    }
    for variant in _GENERATION_VARIANTS:
        variant_dir = target / variant
        _atomic_write_jsonl(variant_dir / "predictions.jsonl", predictions[variant])
        _atomic_write_json(
            variant_dir / "generation-results.json",
            {
                "variant": variant,
                "attempts": [item for item in attempts if item["variant"] == variant],
            },
        )
    _write_harness_handoff(target, predictions)
    return payload


def _validate_formal_generation_identity(
    instances: tuple[SweBenchInstance, ...],
    context_profile: ContextStressProfile,
    dataset_identity: dict[str, str],
) -> None:
    if len(instances) != 10:
        raise ValueError("formal context-stress-10 generation requires exactly 10 tasks")
    if dataset_identity.get("selection_id") != "context-stress-10":
        raise ValueError("formal generation requires the context-stress-10 selection")
    dataset_fingerprint = dataset_identity.get("dataset_fingerprint", "")
    if not re.fullmatch(r"[0-9a-f]{64}", dataset_fingerprint):
        raise ValueError("formal generation requires a SHA-256 dataset fingerprint")
    expected_selection_fingerprint = _hash_json([instance.instance_id for instance in instances])
    if dataset_identity.get("selection_fingerprint") != expected_selection_fingerprint:
        raise ValueError("formal generation selection fingerprint does not match ordered tasks")
    snapshot_value = dataset_identity.get("snapshot_dir")
    if not isinstance(snapshot_value, str) or not snapshot_value:
        raise ValueError("formal generation requires the immutable snapshot directory")
    snapshot_dir = Path(snapshot_value).resolve()
    metadata = _read_json_object(snapshot_dir / "metadata.json")
    if metadata.get("dataset_fingerprint") != dataset_fingerprint:
        raise ValueError("formal snapshot metadata fingerprint does not match experiment")
    selections = metadata.get("selections")
    expected_ids = [instance.instance_id for instance in instances]
    if not isinstance(selections, dict) or selections.get("context-stress-10") != expected_ids:
        raise ValueError("formal snapshot context-stress-10 IDs do not match scheduled tasks")
    raw_records = _decode_records((snapshot_dir / "dataset.json").read_text(encoding="utf-8"))
    snapshot_fingerprint = hashlib.sha256(
        json.dumps(
            raw_records,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if snapshot_fingerprint != dataset_fingerprint:
        raise ValueError("formal snapshot content fingerprint does not match metadata")
    snapshot_instances = load_swebench_selection(snapshot_dir, selection="context-stress-10")
    if snapshot_instances != instances:
        raise ValueError("formal scheduled task contents differ from the frozen snapshot")
    fixed_manifest = _read_json_object(
        _runtime_root()
        / "benchmarks"
        / "swebench-lite-v1"
        / "selections"
        / "context-stress-10.json"
    )
    if (
        fixed_manifest.get("dataset_fingerprint") != dataset_fingerprint
        or fixed_manifest.get("instance_ids") != expected_ids
        or fixed_manifest.get("selection_fingerprint") != expected_selection_fingerprint
    ):
        raise ValueError("formal tasks do not match the version-controlled fixed selection")
    if (
        context_profile.profile_id != "stress-32k-v1"
        or context_profile.input_budget_tokens != 32_768
        or context_profile.output_reserve_tokens != 4_096
    ):
        raise ValueError("swebench-lite-v1 formal runs require the stress-32k-v1 profile")


def _load_resumable_experiment(target: Path, expected: dict[str, Any]) -> dict[str, Any]:
    payload = _read_json_object(target / "experiment.json")
    for field in (
        "artifact_schema_version",
        "formal",
        "runtime_identity",
        "dataset_identity",
        "environment_identity",
        "context_profile",
        "configuration_identity",
        "instance_ids",
        "problem_statement_sha256",
    ):
        if payload.get(field) != expected[field]:
            raise ValueError(f"cannot resume: experiment {field} identity changed")
    terminal_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for metadata_path in target.glob("*/attempts/*/metadata.json"):
        metadata = _read_json_object(metadata_path)
        if metadata.get("state") == "running":
            raise ValueError(f"cannot resume stranded running attempt: {metadata_path}")
        metadata_variant = metadata.get("variant")
        metadata_instance = metadata.get("instance_id")
        if metadata.get("state") in {"completed", "agent_error"}:
            if not isinstance(metadata_variant, str) or not isinstance(metadata_instance, str):
                raise ValueError(f"cannot resume invalid terminal metadata: {metadata_path}")
            terminal_rows[(metadata_variant, metadata_instance)] = metadata
    attempts = payload.get("attempts")
    if not isinstance(attempts, list):
        raise ValueError("cannot resume: experiment attempts must be an array")
    expected_ids = set(expected["instance_ids"])
    seen: set[tuple[str, str]] = set()
    for item in attempts:
        if not isinstance(item, dict):
            raise ValueError("cannot resume: attempt must be an object")
        variant = item.get("variant")
        instance_id = item.get("instance_id")
        if (
            not isinstance(variant, str)
            or variant not in _GENERATION_VARIANTS
            or not isinstance(instance_id, str)
            or instance_id not in expected_ids
            or item.get("state") not in {"completed", "agent_error"}
            or not isinstance(item.get("patch_path"), str)
            or not isinstance(item.get("patch_sha256"), str)
        ):
            raise ValueError(f"cannot resume invalid terminal attempt: {variant}/{instance_id}")
        key = (variant, instance_id)
        if key in seen:
            raise ValueError(f"cannot resume duplicate terminal attempt: {key}")
        if terminal_rows.get(key) != item:
            raise ValueError(f"cannot resume divergent terminal attempt metadata: {key}")
        _load_attempt_patch(target, item)
        seen.add(key)
    if set(terminal_rows) != seen:
        raise ValueError(
            "cannot resume: terminal attempt metadata and experiment manifest disagree"
        )
    return payload


def import_swebench_harness_results(
    experiment_dir: str | Path,
    *,
    variant: str,
    harness_results_dir: str | Path,
    harness_revision: str,
    formal: bool = True,
) -> dict[str, Any]:
    """Import one exact set of official per-instance ``report.json`` outcomes."""

    if variant not in _GENERATION_VARIANTS:
        raise ValueError(f"invalid SWE-bench variant: {variant}")
    revision = harness_revision.strip()
    run_id = _expected_harness_run_id(Path(experiment_dir).resolve(), variant)
    if formal and (not revision or revision.lower() == "unknown"):
        raise ValueError("formal harness imports require an exact harness revision")
    root = Path(experiment_dir).resolve()
    experiment = _read_json_object(root / "experiment.json")
    expected = _instance_id_set(experiment.get("instance_ids"), "experiment")
    reports_root = Path(harness_results_dir).resolve()
    if not reports_root.is_dir():
        raise FileNotFoundError(f"official harness result directory not found: {reports_root}")

    outcomes: dict[str, bool] = {}
    sources: dict[str, str] = {}
    for report_path in sorted(reports_root.rglob("report.json")):
        if formal and run_id not in report_path.parts:
            raise ValueError(
                f"official report path is not under expected run ID {run_id}: {report_path}"
            )
        report = _read_json_object(report_path)
        for instance_id, result in report.items():
            if instance_id in outcomes:
                raise ValueError(f"duplicate official harness outcome: {instance_id}")
            if instance_id not in expected:
                raise ValueError(f"unexpected official harness outcome: {instance_id}")
            if not isinstance(result, dict) or not isinstance(result.get("resolved"), bool):
                raise ValueError(
                    f"official harness outcome for {instance_id} requires boolean resolved"
                )
            outcomes[instance_id] = result["resolved"]
            sources[instance_id] = str(report_path)
    missing = sorted(expected - outcomes.keys())
    if missing:
        raise ValueError(f"missing official harness outcomes: {', '.join(missing)}")

    prediction_path = root / variant / "predictions.jsonl"
    prediction_sha256 = (
        hashlib.sha256(prediction_path.read_bytes()).hexdigest()
        if prediction_path.is_file()
        else None
    )
    if formal:
        harness_request = _read_json_object(root / "harness-request.json")
        request_variants = harness_request.get("variants")
        request_variant = (
            request_variants.get(variant) if isinstance(request_variants, dict) else None
        )
        if (
            not isinstance(request_variant, dict)
            or request_variant.get("predictions_sha256") != prediction_sha256
            or request_variant.get("run_id") != run_id
        ):
            raise ValueError("official import does not match the frozen harness request")
    dataset_identity = experiment.get("dataset_identity")
    dataset_fingerprint = (
        dataset_identity.get("dataset_fingerprint") if isinstance(dataset_identity, dict) else None
    )
    command = _official_harness_command(root, variant=variant, run_id=run_id)
    payload = {
        "artifact_schema_version": 1,
        "variant": variant,
        "formal": formal,
        "outcomes": {instance_id: outcomes[instance_id] for instance_id in sorted(outcomes)},
        "sources": {instance_id: sources[instance_id] for instance_id in sorted(sources)},
        "harness_identity": {
            "revision": revision or "unknown",
            "dataset_fingerprint": dataset_fingerprint,
        },
        "run_identity": {
            "run_id": run_id,
            "command": command,
            "predictions_sha256": prediction_sha256,
        },
        "import_source": {
            "results_directory": str(reports_root),
        },
    }
    _atomic_write_json(root / "imported-harness-results" / f"{variant}.json", payload)
    return payload


def compare_swebench_experiment(experiment_dir: str | Path) -> dict[str, Any]:
    """Create the paired pass@1 and provider-input-token comparison report."""

    root = Path(experiment_dir).resolve()
    experiment = _read_json_object(root / "experiment.json")
    instance_ids = tuple(sorted(_instance_id_set(experiment.get("instance_ids"), "experiment")))
    denominator = len(instance_ids)
    imported = {
        variant: _read_json_object(root / "imported-harness-results" / f"{variant}.json")
        for variant in _GENERATION_VARIANTS
    }
    if experiment.get("formal") and any(not item.get("formal") for item in imported.values()):
        raise ValueError("formal comparison requires two formal harness imports")
    revisions = [
        item.get("harness_identity", {}).get("revision")
        if isinstance(item.get("harness_identity"), dict)
        else None
        for item in imported.values()
    ]
    if experiment.get("formal") and (not revisions[0] or revisions[0] != revisions[1]):
        raise ValueError("formal comparison requires identical harness identities")
    _validate_import_bindings(root, experiment, imported)

    attempts = experiment.get("attempts")
    if not isinstance(attempts, list):
        raise ValueError("experiment attempts must be an array")
    attempts_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for attempt in attempts:
        if not isinstance(attempt, dict):
            raise ValueError("experiment attempt must be an object")
        key = (str(attempt.get("variant")), str(attempt.get("instance_id")))
        if key in attempts_by_key:
            raise ValueError(f"duplicate experiment attempt: {key[0]}/{key[1]}")
        attempts_by_key[key] = attempt
    if experiment.get("formal"):
        _validate_terminal_attempt_artifacts(root, attempts)

    variants: dict[str, dict[str, Any]] = {}
    all_usage_complete = True
    for variant in _GENERATION_VARIANTS:
        outcomes = imported[variant].get("outcomes")
        if not isinstance(outcomes, dict) or set(outcomes) != set(instance_ids):
            raise ValueError(f"{variant} harness outcomes do not match the scheduled task set")
        if any(not isinstance(value, bool) for value in outcomes.values()):
            raise ValueError(f"{variant} harness outcomes must be booleans")
        variant_attempts: list[dict[str, Any]] = []
        effective_outcomes: dict[str, bool] = {}
        input_tokens: list[int] = []
        output_tokens: list[int] = []
        usage_complete = True
        for instance_id in instance_ids:
            attempt = attempts_by_key.get((variant, instance_id))
            if attempt is None:
                raise ValueError(f"missing experiment attempt: {variant}/{instance_id}")
            variant_attempts.append(attempt)
            effective_outcomes[instance_id] = bool(
                outcomes[instance_id]
                and attempt.get("state") == "completed"
                and isinstance(attempt.get("patch_bytes"), int)
                and attempt.get("patch_bytes", 0) > 0
            )
            usage = attempt.get("actual_usage")
            if (
                attempt.get("usage_source") != "provider_reported"
                or not isinstance(usage, dict)
                or not isinstance(usage.get("input_tokens"), int)
                or isinstance(usage.get("input_tokens"), bool)
            ):
                usage_complete = False
                continue
            input_tokens.append(usage["input_tokens"])
            output_value = usage.get("output_tokens")
            if isinstance(output_value, int) and not isinstance(output_value, bool):
                output_tokens.append(output_value)
        all_usage_complete = all_usage_complete and usage_complete
        resolved = sum(effective_outcomes.values())
        variants[variant] = {
            "resolved": resolved,
            "scheduled": denominator,
            "pass_at_1": resolved / denominator,
            "official_outcomes": {
                instance_id: bool(outcomes[instance_id]) for instance_id in instance_ids
            },
            "effective_outcomes": effective_outcomes,
            "provider_usage_complete": usage_complete,
            "average_provider_input_tokens": (
                sum(input_tokens) / denominator if usage_complete else None
            ),
            "average_provider_output_tokens": (
                sum(output_tokens) / denominator
                if usage_complete and len(output_tokens) == denominator
                else None
            ),
        }

    baseline = variants["full-history"]
    optimized = variants["optimized"]
    baseline_average = baseline["average_provider_input_tokens"]
    optimized_average = optimized["average_provider_input_tokens"]
    reduction = (
        (baseline_average - optimized_average) / baseline_average
        if all_usage_complete and isinstance(baseline_average, int | float) and baseline_average > 0
        else None
    )
    pass_improved = optimized["pass_at_1"] > baseline["pass_at_1"]
    tokens_improved = reduction is not None and reduction > 0
    claim_eligible = bool(
        experiment.get("formal") and pass_improved and tokens_improved and all_usage_complete
    )
    statement = None
    if claim_eligible:
        statement = (
            f"在固定 {denominator} 任务套件、{_profile_label(experiment)} 配置下，"
            f"将 PaiCLI pass@1 从 {baseline['pass_at_1']:.1%} 提升至 "
            f"{optimized['pass_at_1']:.1%}，平均模型输入 token 消耗降低 {reduction:.1%}。"
        )
    paired_results = []
    for instance_id in instance_ids:
        row: dict[str, Any] = {"instance_id": instance_id}
        for variant in _GENERATION_VARIANTS:
            attempt = attempts_by_key[(variant, instance_id)]
            usage = attempt.get("actual_usage")
            row[variant] = {
                "official_resolved": variants[variant]["official_outcomes"][instance_id],
                "resolved": variants[variant]["effective_outcomes"][instance_id],
                "state": attempt.get("state"),
                "input_tokens": usage.get("input_tokens") if isinstance(usage, dict) else None,
            }
        paired_results.append(row)
    payload = {
        "artifact_schema_version": 1,
        "instance_ids": list(instance_ids),
        "variants": variants,
        "pass_at_1_change_points": (optimized["pass_at_1"] - baseline["pass_at_1"]) * 100,
        "input_token_reduction": reduction,
        "provider_usage_complete": all_usage_complete,
        "paired_results": paired_results,
        "claim_eligible": claim_eligible,
        "suggested_resume_statement": statement,
    }
    _atomic_write_json(root / "comparison.json", payload)
    _atomic_write_text(root / "report.md", _render_comparison_report(payload))
    return payload


def _run_generation_attempt(
    instance: SweBenchInstance,
    *,
    variant: str,
    cache_root: Path,
    experiment_dir: Path,
    base_config: PaiCliConfig,
    context_profile: ContextStressProfile,
    client_factory: Callable[[SweBenchInstance, str, PaiCliConfig], Any],
    usage_source: str,
    keep_workspace: bool,
) -> dict[str, Any]:
    attempt_dir = experiment_dir / variant / "attempts" / instance.instance_id
    run_dir = experiment_dir / "workspaces" / instance.instance_id / variant
    attempt_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(
        attempt_dir / "metadata.json",
        {"instance_id": instance.instance_id, "variant": variant, "state": "running"},
    )
    workspace = materialize_swebench_workspace(
        instance,
        cache_root=cache_root,
        destination=run_dir,
    )
    config = copy.deepcopy(base_config)
    config.policy.audit_log_path = str(attempt_dir / "audit")
    config.context.tool_result_storage_dir = str(workspace.path / ".paicli" / "tool_results")
    client = client_factory(instance, variant, config)
    secrets = {value for value in (config.llm.api_key,) if value}
    manager_factory = (
        full_history_context_manager_factory(context_profile) if variant == "full-history" else None
    )
    started = time.monotonic()
    agent_result = asyncio.run(
        _execute_generation_agent(
            instance.problem_statement,
            workspace.path,
            config,
            client,
            context_manager_factory=manager_factory,
            snapshot_dir=attempt_dir / "snapshots",
            secrets=secrets,
        )
    )
    patch = _collect_final_tree_patch(workspace)
    model_patch = patch
    status = "completed" if agent_result["error"] is None else "agent_error"
    if _contains_sensitive_text(patch, secrets):
        patch = ""
        model_patch = ""
        status = "agent_error"
        agent_result["error"] = "Agent patch contained a credential; unsafe patch omitted"
    error_category = None
    if agent_result["error"] and "ContextWindowExceededError" in agent_result["error"]:
        error_category = "context_limit_exceeded"
    elif agent_result["error"] and "credential" in agent_result["error"].lower():
        error_category = "credential_detected"
    _atomic_write_text(attempt_dir / "patch.diff", patch)
    _atomic_write_text(attempt_dir / "response.txt", agent_result["response"])
    _atomic_write_jsonl(attempt_dir / "events.jsonl", agent_result["events"])
    _atomic_write_jsonl(
        attempt_dir / "context-events.jsonl",
        [
            event
            for event in agent_result["events"]
            if str(event.get("type", "")).startswith("context")
        ],
    )
    apply_check = _local_apply_check(instance, cache_root, patch, attempt_dir / "apply-check")
    _atomic_write_text(attempt_dir / "local-apply-check.log", apply_check["output"])
    row = {
        "instance_id": instance.instance_id,
        "variant": variant,
        "state": status,
        "error": agent_result["error"],
        "error_category": error_category,
        "model": getattr(client, "model_name", config.llm.model),
        "provider": getattr(client, "provider_name", config.llm.provider),
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "turns": agent_result["turns"],
        "actual_usage": agent_result["actual_usage"],
        "usage_source": usage_source if agent_result["actual_usage"] else None,
        "context_reductions": agent_result["context_reductions"],
        "local_apply_check": apply_check["ok"],
        "patch_bytes": len(patch.encode("utf-8")),
        "patch_sha256": hashlib.sha256(model_patch.encode("utf-8")).hexdigest(),
        "patch_path": str((attempt_dir / "patch.diff").relative_to(experiment_dir)),
    }
    _atomic_write_json(attempt_dir / "metadata.json", row)
    if not keep_workspace:
        shutil.rmtree(run_dir, ignore_errors=True)
    return row


async def _execute_generation_agent(
    prompt: str,
    workspace: Path,
    config: PaiCliConfig,
    client: Any,
    *,
    context_manager_factory: Callable[..., ContextManager] | None,
    snapshot_dir: Path,
    secrets: set[str],
) -> dict[str, Any]:
    from paicli.agent import QueryEngine

    engine = QueryEngine(
        llm_client=client,
        tool_registry=_generation_tool_registry(),
        config=config,
        cwd=str(workspace),
        context_manager_factory=context_manager_factory,
    )
    response = ""
    events: list[dict[str, Any]] = []
    input_tokens = 0
    output_tokens = 0
    usage_seen = False
    reductions = 0
    turns = 0
    error: str | None = None
    with _temporary_environment(
        {
            "PAICLI_SNAPSHOT_DIR": str(snapshot_dir),
            "PAICLI_SNAPSHOT_ENABLED": "true",
        }
    ):
        try:
            async for event in engine.ask(prompt):
                event_type = str(event.get("type") or "")
                if event_type == "text_delta":
                    response += _redact_sensitive_text(str(event.get("text") or ""), secrets)
                elif event_type == "usage":
                    usage = dict(event.get("usage") or {})
                    input_tokens += int(usage.get("input_tokens") or 0)
                    output_tokens += int(usage.get("output_tokens") or 0)
                    usage_seen = True
                elif event_type == "context_reduced":
                    reductions += 1
                elif event_type == "done":
                    turns = int(event.get("total_turns") or 0)
                elif event_type == "error":
                    value = event.get("error")
                    error = f"{type(value).__name__}: {value}"
                safe = _safe_event(event, secrets=secrets)
                if safe is not None:
                    events.append(safe)
        except Exception as exc:  # noqa: BLE001 - model boundary becomes attempt evidence
            error = _redact_sensitive_text(f"{type(exc).__name__}: {exc}", secrets)
    return {
        "response": response,
        "events": events,
        "actual_usage": (
            {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            }
            if usage_seen
            else None
        ),
        "context_reductions": reductions,
        "turns": turns,
        "error": error,
    }


def _generation_config(profile: ContextStressProfile, output_dir: Path) -> PaiCliConfig:
    from paicli.config import load_config

    config = load_config(project_root=_runtime_root())
    config.llm.temperature = 0.0
    config.llm.max_tokens = 4096
    config.llm.context_window = profile.input_budget_tokens + profile.output_reserve_tokens
    config.context.utilization_rate = 1.0
    config.context.output_reserve_tokens = profile.output_reserve_tokens
    config.agent.max_turns = 60
    config.agent.max_tool_calls = 100
    config.agent.max_elapsed_seconds = 1800.0
    config.agent.max_total_tokens = 300_000
    config.policy.hitl_mode = "never"
    config.policy.require_approval_for_writes = False
    config.policy.audit_log_path = str(output_dir / "audit")
    config.features.mcp = False
    config.features.skill = False
    config.features.memory = False
    config.memory.long_term_enabled = False
    config.tools.enabled = sorted(_TOOL_PROFILE)
    config.tools.disabled = sorted(
        tool.name for tool in get_builtin_tools() if tool.name not in _TOOL_PROFILE
    )
    return config


def _generation_configuration_identity(config: PaiCliConfig) -> dict[str, Any]:
    llm_retry = config.retry.resolve("llm")
    tool_retry = config.retry.resolve("tools")
    details = {
        "provider": config.llm.provider,
        "model": config.llm.model,
        "temperature": config.llm.temperature,
        "max_output_tokens": config.llm.max_tokens,
        "base_url_sha256": hashlib.sha256((config.llm.base_url or "").encode()).hexdigest(),
        "agent_budget": {
            "profile_id": "swe-lite-agent-v1",
            "max_turns": config.agent.max_turns,
            "max_tool_calls": config.agent.max_tool_calls,
            "max_elapsed_seconds": config.agent.max_elapsed_seconds,
            "max_total_tokens": config.agent.max_total_tokens,
        },
        "tool_profile_id": "network-tool-free-coding-v1",
        "tools": sorted(_TOOL_PROFILE),
        "retry_policy": {
            "llm": {
                "enabled": llm_retry.enabled,
                "max_retries": llm_retry.max_retries,
                "base_delay": llm_retry.base_delay,
                "max_delay": llm_retry.max_delay,
                "max_retry_after": llm_retry.max_retry_after,
            },
            "tools": {
                "enabled": tool_retry.enabled,
                "max_retries": tool_retry.max_retries,
                "base_delay": tool_retry.base_delay,
                "max_delay": tool_retry.max_delay,
                "max_retry_after": tool_retry.max_retry_after,
            },
        },
        "environment": {
            "filesystem_isolation": False,
            "network_isolation": False,
            "reference_data_confidentiality": False,
            "agent_test_environment": "host_unmanaged",
        },
    }
    return {**details, "fingerprint": _hash_json(details)}


def _live_generation_client(
    _instance: SweBenchInstance,
    _variant: str,
    config: PaiCliConfig,
) -> Any:
    return create_llm_client(
        config.llm,
        retry_policy=config.retry.resolve("llm"),
        retry_audit_path=config.policy.audit_log_path,
        retry_cwd=str(_runtime_root()),
    )


def _generation_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_all([tool for tool in get_builtin_tools() if tool.name in _TOOL_PROFILE])
    return registry


def _collect_final_tree_patch(workspace: SweBenchWorkspace) -> str:
    with tempfile.TemporaryDirectory(prefix="paicli-swebench-index-") as temporary:
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(Path(temporary) / "index")
        _run_git_env(workspace.path, env, "read-tree", workspace.base_commit)
        _run_git_env(
            workspace.path,
            env,
            "add",
            "-A",
            "-f",
            "--",
            ".",
            ":(exclude,glob)**/.paicli/**",
            ":(exclude,glob)**/.pytest_cache/**",
            ":(exclude,glob)**/__pycache__/**",
            ":(exclude,glob)**/*.pyc",
            ":(exclude,glob)**/*.pyo",
        )
        return _run_git_env(
            workspace.path,
            env,
            "diff",
            "--cached",
            "--binary",
            workspace.base_commit,
        ).stdout


def _local_apply_check(
    instance: SweBenchInstance,
    cache_root: Path,
    patch: str,
    destination: Path,
) -> dict[str, Any]:
    if not patch:
        return {"ok": True, "output": "empty patch\n"}
    workspace = materialize_swebench_workspace(
        instance,
        cache_root=cache_root,
        destination=destination,
    )
    process = subprocess.run(
        ["git", "apply", "--check", "--binary", "-"],
        cwd=workspace.path,
        input=patch,
        text=True,
        capture_output=True,
        check=False,
    )
    shutil.rmtree(workspace.path, ignore_errors=True)
    return {"ok": process.returncode == 0, "output": process.stdout + process.stderr}


def _run_git_env(
    cwd: Path,
    env: dict[str, str],
    *args: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )


def _write_harness_handoff(
    experiment_dir: Path,
    predictions: dict[str, list[dict[str, str]]],
) -> None:
    experiment = _read_json_object(experiment_dir / "experiment.json")
    dataset_identity = experiment.get("dataset_identity")
    if not isinstance(dataset_identity, dict):
        raise ValueError("experiment is missing dataset identity")
    request = {
        "dataset_identity": dataset_identity,
        "context_profile": experiment.get("context_profile"),
        "expected_instance_ids_sha256": _hash_json(experiment.get("instance_ids")),
        "variants": {
            variant: {
                "predictions_sha256": hashlib.sha256(
                    "".join(
                        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                        for row in rows
                    ).encode()
                ).hexdigest(),
                "instance_ids": [row["instance_id"] for row in rows],
                "run_id": _expected_harness_run_id(experiment_dir, variant),
                "command": _official_harness_command(
                    experiment_dir,
                    variant=variant,
                    run_id=_expected_harness_run_id(experiment_dir, variant),
                ),
            }
            for variant, rows in predictions.items()
        },
    }
    _atomic_write_json(experiment_dir / "harness-request.json", request)
    commands = []
    for variant in _GENERATION_VARIANTS:
        commands.append(
            _official_harness_command(
                experiment_dir,
                variant=variant,
                run_id=_expected_harness_run_id(experiment_dir, variant),
            )
        )
    _atomic_write_text(experiment_dir / "harness-command.txt", "\n\n".join(commands) + "\n")


def _official_harness_command(experiment_dir: Path, *, variant: str, run_id: str) -> str:
    experiment = _read_json_object(experiment_dir / "experiment.json")
    dataset_identity = experiment.get("dataset_identity")
    snapshot_dir = (
        dataset_identity.get("snapshot_dir") if isinstance(dataset_identity, dict) else None
    )
    dataset_argument = (
        str(Path(snapshot_dir) / "dataset.json")
        if isinstance(snapshot_dir, str)
        else "<DATASET_JSON>"
    )
    return subprocess.list2cmdline(
        [
            "python",
            "-m",
            "swebench.harness.run_evaluation",
            "--dataset_name",
            dataset_argument,
            "--predictions_path",
            str(experiment_dir / variant / "predictions.jsonl"),
            "--max_workers",
            "1",
            "--run_id",
            run_id,
        ]
    )


def _expected_harness_run_id(experiment_dir: Path, variant: str) -> str:
    experiment_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", experiment_dir.name).strip("-.")
    if not experiment_name:
        experiment_name = "experiment"
    prediction_path = experiment_dir / variant / "predictions.jsonl"
    if not prediction_path.is_file():
        raise FileNotFoundError(
            f"prediction artifact not found for harness identity: {prediction_path}"
        )
    experiment = _read_json_object(experiment_dir / "experiment.json")
    identity = {
        "dataset_identity": experiment.get("dataset_identity"),
        "runtime_identity": experiment.get("runtime_identity"),
        "configuration_identity": experiment.get("configuration_identity"),
        "context_profile": experiment.get("context_profile"),
        "instance_ids": experiment.get("instance_ids"),
        "variant": variant,
        "predictions_sha256": hashlib.sha256(prediction_path.read_bytes()).hexdigest(),
    }
    return f"paicli-{experiment_name}-{variant}-{_hash_json(identity)[:12]}"


def _validate_import_bindings(
    experiment_dir: Path,
    experiment: dict[str, Any],
    imported: dict[str, dict[str, Any]],
) -> None:
    if not experiment.get("formal"):
        return
    dataset_identity = experiment.get("dataset_identity")
    if not isinstance(dataset_identity, dict):
        raise ValueError("formal experiment is missing dataset identity")
    dataset_fingerprint = dataset_identity.get("dataset_fingerprint")
    run_ids: set[str] = set()
    for variant in _GENERATION_VARIANTS:
        item = imported[variant]
        harness_identity = item.get("harness_identity")
        run_identity = item.get("run_identity")
        if not isinstance(harness_identity, dict) or not isinstance(run_identity, dict):
            raise ValueError(f"{variant} import is missing formal harness/run identity")
        if harness_identity.get("dataset_fingerprint") != dataset_fingerprint:
            raise ValueError(f"{variant} import dataset fingerprint does not match experiment")
        prediction_path = experiment_dir / variant / "predictions.jsonl"
        if not prediction_path.is_file():
            raise FileNotFoundError(f"formal prediction artifact not found: {prediction_path}")
        prediction_sha256 = hashlib.sha256(prediction_path.read_bytes()).hexdigest()
        if run_identity.get("predictions_sha256") != prediction_sha256:
            raise ValueError(f"{variant} imported run is not bound to current predictions")
        run_id = run_identity.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError(f"{variant} imported run requires a run ID")
        if run_identity.get("command") != _official_harness_command(
            experiment_dir, variant=variant, run_id=run_id
        ):
            raise ValueError(f"{variant} imported harness command identity is invalid")
        run_ids.add(run_id)
    if len(run_ids) != len(_GENERATION_VARIANTS):
        raise ValueError("formal variants require distinct official harness run IDs")


def _runtime_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _runtime_identity() -> dict[str, Any]:
    root = _runtime_root()
    revision = _run_git(root, "rev-parse", "HEAD").stdout.strip()
    source_tree = _run_git(root, "rev-parse", "HEAD^{tree}").stdout.strip()
    dirty = bool(_run_git(root, "status", "--porcelain").stdout.strip())
    return {"revision": revision, "source_tree": source_tree, "dirty": dirty}


def _environment_identity() -> dict[str, str]:
    identity = {
        "operating_system": platform.system(),
        "os_release": platform.release(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "git": _run_git(None, "--version").stdout.strip(),
    }
    for package in (
        "paicli-python",
        "dulwich",
        "httpx",
        "jsonschema",
        "mcp",
        "pillow",
        "prompt-toolkit",
        "rich",
        "textual",
        "typer",
    ):
        with contextlib.suppress(importlib.metadata.PackageNotFoundError):
            identity[f"package:{package}"] = importlib.metadata.version(package)
    return identity


@contextlib.contextmanager
def _temporary_environment(values: dict[str, str]):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _safe_event(event: dict[str, Any], *, secrets: set[str]) -> dict[str, Any] | None:
    if event.get("type") == "thinking_delta":
        return None
    safe: dict[str, Any] = {}
    for key, value in event.items():
        if key in {"messages", "thinking", "reasoning_content"}:
            continue
        safe[key] = _sanitize_event_value(value, secrets=secrets)
    return safe


def _sanitize_event_value(value: Any, *, secrets: set[str]) -> Any:
    if isinstance(value, BaseException):
        return _redact_sensitive_text(f"{type(value).__name__}: {value}", secrets)
    if isinstance(value, str):
        return _redact_sensitive_text(value, secrets)[:2000]
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, list):
        return [_sanitize_event_value(item, secrets=secrets) for item in value[:50]]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:50]:
            name = str(key)
            if name.lower() in {"api_key", "authorization", "password", "secret", "token"}:
                result[name] = "[REDACTED]"
            else:
                result[name] = _sanitize_event_value(item, secrets=secrets)
        return result
    return _redact_sensitive_text(str(value), secrets)[:2000]


def _contains_sensitive_text(value: str, secrets: set[str]) -> bool:
    return _redact_sensitive_text(value, secrets) != value


def _redact_sensitive_text(value: str, secrets: set[str]) -> str:
    redacted = value
    for secret in sorted(secrets, key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    redacted = re.sub(
        r"(?i)bearer\s+[A-Za-z0-9._~+\-/=]+",
        "Bearer [REDACTED]",
        redacted,
    )
    redacted = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", redacted)
    return re.sub(
        (
            r"(?i)(\b(?:api[_-]?key|authorization|password|secret|token)\b"
            r"\s*[:=]\s*[\"']?)([A-Za-z0-9._~+\-/=]{4,})"
        ),
        r"\1[REDACTED]",
        redacted,
    )


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _atomic_write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    _atomic_write_text(
        path,
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
    )


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _hash_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"required SWE-bench artifact not found: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"SWE-bench artifact must be an object: {path}")
    return value


def _load_attempt_patch(experiment_dir: Path, attempt: dict[str, Any]) -> str:
    relative = attempt.get("patch_path")
    expected_sha256 = attempt.get("patch_sha256")
    if not isinstance(relative, str) or not isinstance(expected_sha256, str):
        raise ValueError("attempt is missing patch path or fingerprint")
    path = (experiment_dir / relative).resolve()
    if experiment_dir.resolve() not in path.parents:
        raise ValueError(f"attempt patch escapes experiment directory: {relative}")
    if not path.is_file():
        raise FileNotFoundError(f"attempt patch artifact not found: {path}")
    patch = path.read_text(encoding="utf-8")
    if hashlib.sha256(patch.encode("utf-8")).hexdigest() != expected_sha256:
        raise ValueError(f"attempt patch fingerprint mismatch: {path}")
    return patch


def _validate_terminal_attempt_artifacts(
    experiment_dir: Path, attempts: list[dict[str, Any]]
) -> None:
    for attempt in attempts:
        variant = attempt.get("variant")
        instance_id = attempt.get("instance_id")
        if not isinstance(variant, str) or not isinstance(instance_id, str):
            raise ValueError("formal attempt is missing variant or instance identity")
        metadata_path = experiment_dir / variant / "attempts" / instance_id / "metadata.json"
        if _read_json_object(metadata_path) != attempt:
            raise ValueError(f"formal terminal attempt metadata diverged: {variant}/{instance_id}")
        _load_attempt_patch(experiment_dir, attempt)


def _instance_id_set(value: Any, source: str) -> set[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
    ):
        raise ValueError(f"{source} instance_ids must be a non-empty string array")
    result = set(value)
    if len(result) != len(value):
        raise ValueError(f"{source} instance_ids contains duplicates")
    return result


def _profile_label(experiment: dict[str, Any]) -> str:
    profile = experiment.get("context_profile")
    if isinstance(profile, dict):
        profile_id = profile.get("profile_id")
        if isinstance(profile_id, str) and profile_id:
            return profile_id
    return "固定上下文"


def _render_comparison_report(payload: dict[str, Any]) -> str:
    baseline = payload["variants"]["full-history"]
    optimized = payload["variants"]["optimized"]
    reduction = payload["input_token_reduction"]
    reduction_text = f"{reduction:.1%}" if isinstance(reduction, int | float) else "不可用"
    statement = payload["suggested_resume_statement"] or "不满足自动生成改进表述的证据门槛。"
    lines = [
        "# SWE-bench Lite A/B Comparison",
        "",
        "| Variant | Resolved | pass@1 | Avg provider input tokens |",
        "| --- | ---: | ---: | ---: |",
        (
            f"| Full history | {baseline['resolved']}/{baseline['scheduled']} | "
            f"{baseline['pass_at_1']:.1%} | "
            f"{baseline['average_provider_input_tokens'] or '不可用'} |"
        ),
        (
            f"| Optimized | {optimized['resolved']}/{optimized['scheduled']} | "
            f"{optimized['pass_at_1']:.1%} | "
            f"{optimized['average_provider_input_tokens'] or '不可用'} |"
        ),
        "",
        f"- pass@1 change: {payload['pass_at_1_change_points']:+.1f} percentage points",
        f"- provider input-token reduction: {reduction_text}",
        f"- claim eligible: {str(payload['claim_eligible']).lower()}",
        "",
        "## Suggested resume statement",
        "",
        statement,
        "",
        "## Per-instance paired results",
        "",
        "| Instance | Full history | Optimized | Full input | Optimized input |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for row in payload["paired_results"]:
        baseline_row = row["full-history"]
        optimized_row = row["optimized"]
        baseline_input = (
            baseline_row["input_tokens"] if baseline_row["input_tokens"] is not None else "不可用"
        )
        optimized_input = (
            optimized_row["input_tokens"] if optimized_row["input_tokens"] is not None else "不可用"
        )
        lines.append(
            f"| {row['instance_id']} | {str(baseline_row['resolved']).lower()} | "
            f"{str(optimized_row['resolved']).lower()} | "
            f"{baseline_input} | {optimized_input} |"
        )
    lines.append("")
    return "\n".join(lines)


def _run_git(cwd: Path | None, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )


def _selection_digest(seed: str, instance_id: str) -> str:
    return hashlib.sha256(f"{seed}\0{instance_id}".encode()).hexdigest()


def _decode_records(text: str) -> list[Any]:
    stripped = text.lstrip()
    if stripped.startswith("["):
        value = json.loads(text)
        if not isinstance(value, list):
            raise ValueError("SWE-bench JSON source must be an array")
        return value
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _required_text(record: dict[str, Any], field: str, index: int) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"SWE-bench record {index} requires non-empty {field}")
    return value


__all__ = [
    "ContextStressProfile",
    "FullHistoryContextManager",
    "PreparedRepository",
    "SweBenchInstance",
    "SweBenchWorkspace",
    "full_history_context_manager_factory",
    "compare_swebench_experiment",
    "fetch_swebench_dataset",
    "freeze_swebench_selection_manifests",
    "import_swebench_dataset",
    "import_swebench_harness_results",
    "load_context_stress_profile",
    "load_swebench_instances",
    "load_swebench_selection",
    "materialize_swebench_workspace",
    "prepare_swebench_repositories",
    "run_swebench_generation",
    "select_repository_balanced_instances",
]
