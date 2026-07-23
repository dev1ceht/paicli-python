"""Local coding smoke benchmark for PaiCLI's production Agent path."""

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
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from paicli import __version__
from paicli.agent import QueryEngine
from paicli.config import PaiCliConfig, load_config
from paicli.context import ContextManager
from paicli.evaluation.swebench import (
    ContextStressProfile,
    full_history_context_manager_factory,
    load_context_stress_profile,
)
from paicli.llm import create_llm_client
from paicli.tools import ToolRegistry, get_builtin_tools
from paicli.tools.base import ToolResult
from paicli.types import Message

ClientFactory = Callable[["LocalSmokeTask", int, PaiCliConfig], Any]

TOOL_PROFILE_NAME = "network-tool-free-coding-v2"
TOOL_PROFILE = frozenset(
    {
        "apply_patch",
        "edit_file",
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
CONTEXT_VARIANTS = ("full-history", "optimized")
LOCAL_SMOKE_V2_TASK_IDS = frozenset(
    {
        "config-migration",
        "debug-and-fix",
        "dependency-upgrade",
        "health-endpoint",
        "invoice-totals",
        "multi-file-refactor",
        "string-normalize",
    }
)
STRESS_16K_V1_FINGERPRINT = "ef1debf743bcc27e7d3d1f99d2d9698cf13b2610ac550b7308970bb09e8469a0"


@dataclass(frozen=True, slots=True)
class LocalSmokeTask:
    id: str
    prompt: str
    fixture_repo: Path
    acceptance: Path
    provenance: dict[str, str] = field(default_factory=dict)
    pressure_class: str = "normal"
    history: tuple[Message, ...] = ()
    history_fingerprint: str = ""


@dataclass(frozen=True, slots=True)
class FrozenTaskMaterial:
    fixture_files: dict[str, bytes] = field(repr=False)
    acceptance_files: dict[str, bytes] = field(repr=False)


@dataclass(frozen=True, slots=True)
class LocalSmokeSuite:
    suite_id: str
    manifest_path: Path
    verifier_timeout_seconds: int
    tasks: tuple[LocalSmokeTask, ...]
    fingerprint: str
    material_by_task: dict[str, FrozenTaskMaterial] = field(repr=False)


@dataclass(frozen=True, slots=True)
class BenchmarkWorkspace:
    path: Path
    base_commit: str


_MANIFEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "suite_id", "verifier", "tasks"],
    "properties": {
        "schema_version": {"enum": [1, 2]},
        "suite_id": {"type": "string", "minLength": 1},
        "verifier": {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "timeout_seconds"],
            "properties": {
                "kind": {"const": "pytest"},
                "timeout_seconds": {"type": "integer", "minimum": 1},
            },
        },
        "tasks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "prompt", "fixture_repo", "acceptance"],
                "properties": {
                    "id": {
                        "type": "string",
                        "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$",
                    },
                    "prompt": {"type": "string", "minLength": 1},
                    "fixture_repo": {"type": "string", "minLength": 1},
                    "acceptance": {"type": "string", "minLength": 1},
                    "history": {"type": "string", "minLength": 1},
                    "pressure_class": {
                        "enum": ["normal", "medium", "high"],
                    },
                    "provenance": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "project": {"type": "string", "minLength": 1},
                            "revision": {"type": "string", "minLength": 1},
                            "task_id": {"type": "string", "minLength": 1},
                        },
                    },
                },
            },
        },
    },
}


def load_local_smoke_suite(manifest_path: str | Path) -> LocalSmokeSuite:
    """Load a strictly validated local smoke suite and fingerprint its content."""

    path = Path(manifest_path).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(_MANIFEST_SCHEMA).iter_errors(data), key=str)
    if errors:
        raise ValueError(f"invalid local smoke manifest: {errors[0].message}")

    root = path.parent
    schema_version = int(data["schema_version"])
    if schema_version == 2:
        for item in data["tasks"]:
            if "history" not in item or "pressure_class" not in item:
                raise ValueError("local-smoke-v2 tasks require history and pressure_class")
    task_ids = [str(item["id"]) for item in data["tasks"]]
    seen: set[str] = set()
    for task_id in task_ids:
        if task_id in seen:
            raise ValueError(f"duplicate task id: {task_id}")
        seen.add(task_id)
    loaded_tasks: list[LocalSmokeTask] = []
    for item in data["tasks"]:
        task_id = str(item["id"])
        history, history_fingerprint = (
            _load_task_history(root, str(item["history"]), task_id)
            if schema_version == 2
            else ((), "")
        )
        loaded_tasks.append(
            LocalSmokeTask(
                id=task_id,
                prompt=str(item["prompt"]),
                fixture_repo=_resolve_suite_directory(root, str(item["fixture_repo"])),
                acceptance=_resolve_suite_directory(root, str(item["acceptance"])),
                provenance={
                    str(key): str(value) for key, value in item.get("provenance", {}).items()
                },
                pressure_class=str(item.get("pressure_class", "normal")),
                history=history,
                history_fingerprint=history_fingerprint,
            )
        )
    tasks = tuple(loaded_tasks)
    for task in tasks:
        if (
            task.fixture_repo == task.acceptance
            or task.fixture_repo in task.acceptance.parents
            or task.acceptance in task.fixture_repo.parents
        ):
            raise ValueError(
                f"fixture and acceptance directories must not overlap for task: {task.id}"
            )
    material_by_task = {
        task.id: FrozenTaskMaterial(
            fixture_files=_read_material_tree(task.fixture_repo, label="fixture"),
            acceptance_files=_read_material_tree(task.acceptance, label="acceptance"),
        )
        for task in tasks
    }
    fingerprint = _content_fingerprint(data, tasks, material_by_task)
    return LocalSmokeSuite(
        suite_id=str(data["suite_id"]),
        manifest_path=path,
        verifier_timeout_seconds=int(data["verifier"]["timeout_seconds"]),
        tasks=tasks,
        fingerprint=fingerprint,
        material_by_task=material_by_task,
    )


def materialize_benchmark_workspace(
    task: LocalSmokeTask,
    target: str | Path,
    *,
    fixture_files: dict[str, bytes] | None = None,
) -> BenchmarkWorkspace:
    """Copy a fixture into a fresh Git repository and record its baseline."""

    destination = Path(target).resolve()
    if destination.exists():
        raise FileExistsError(f"benchmark workspace already exists: {destination}")
    files = fixture_files or _read_material_tree(task.fixture_repo, label="fixture")
    destination.mkdir(parents=True)
    for relative, content in files.items():
        file_path = destination / Path(relative)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)
    _run_git(destination, "init", "-q")
    exclude = destination / ".git" / "info" / "exclude"
    with exclude.open("a", encoding="utf-8") as file:
        file.write("\n.paicli/\n.pytest_cache/\n__pycache__/\n*.py[cod]\n")
    _run_git(destination, "add", "-A")
    _run_git(
        destination,
        "-c",
        "user.name=PaiCLI Benchmark",
        "-c",
        "user.email=benchmark@paicli.local",
        "commit",
        "--allow-empty",
        "-q",
        "-m",
        "benchmark fixture baseline",
    )
    base_commit = _run_git(destination, "rev-parse", "HEAD").stdout.strip()
    return BenchmarkWorkspace(path=destination, base_commit=base_commit)


def collect_benchmark_patch(workspace: BenchmarkWorkspace) -> str:
    """Return the complete final-tree diff relative to a workspace baseline."""

    with tempfile.TemporaryDirectory(prefix="paicli-benchmark-index-") as temporary:
        index = Path(temporary) / "index"
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(index)
        _run_git(workspace.path, "read-tree", workspace.base_commit, env=env)
        _run_git(
            workspace.path,
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
            env=env,
        )
        return _run_git(
            workspace.path,
            "diff",
            "--cached",
            "--binary",
            workspace.base_commit,
            env=env,
        ).stdout


def run_local_smoke(
    manifest_path: str | Path,
    *,
    output_dir: str | Path,
    repetitions: int = 1,
    client_factory: ClientFactory | None = None,
    allow_unsandboxed: bool = False,
    require_clean_runtime: bool = False,
    keep_workspaces: bool = False,
    compare_contexts: bool = False,
    context_profile: str | Path | None = None,
) -> dict[str, Any]:
    """Run a local smoke suite under an exclusive output-directory lock."""

    target = Path(output_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    with _local_smoke_lock(target):
        return _run_local_smoke(
            manifest_path,
            output_dir=target,
            repetitions=repetitions,
            client_factory=client_factory,
            allow_unsandboxed=allow_unsandboxed,
            require_clean_runtime=require_clean_runtime,
            keep_workspaces=keep_workspaces,
            compare_contexts=compare_contexts,
            context_profile=context_profile,
        )


def _run_local_smoke(
    manifest_path: str | Path,
    *,
    output_dir: str | Path,
    repetitions: int = 1,
    client_factory: ClientFactory | None = None,
    allow_unsandboxed: bool = False,
    require_clean_runtime: bool = False,
    keep_workspaces: bool = False,
    compare_contexts: bool = False,
    context_profile: str | Path | None = None,
) -> dict[str, Any]:
    """Run a local smoke suite after its output directory has been locked."""

    if repetitions < 1:
        raise ValueError("repetitions must be at least one")
    live = client_factory is None
    if live and not allow_unsandboxed:
        raise ValueError("live benchmark execution requires --allow-unsandboxed")
    if compare_contexts and context_profile is None:
        raise ValueError("context comparison requires --context-profile")

    suite = load_local_smoke_suite(manifest_path)
    if compare_contexts and any(not task.history_fingerprint for task in suite.tasks):
        raise ValueError("context comparison requires structured history for every task")
    profile = load_context_stress_profile(context_profile) if context_profile else None
    target = Path(output_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    runtime_identity = _runtime_identity()
    if require_clean_runtime and runtime_identity["dirty"]:
        raise ValueError("--require-clean-runtime rejects the dirty PaiCLI runtime")

    base_config = load_config(project_root=_runtime_root())
    _configure_benchmark(base_config, target, context_profile=profile)
    factory = client_factory or _live_client_factory
    payload: dict[str, Any] = {
        "artifact_schema_version": 1,
        "compare_contexts": compare_contexts,
        "repetitions": repetitions,
        "suite": {"id": suite.suite_id, "fingerprint": suite.fingerprint},
        "runtime_identity": runtime_identity,
        "configuration_identity": _configuration_identity(base_config),
        "environment_identity": _environment_identity(),
        "isolation": {
            "filesystem_isolation": False,
            "network_isolation": False,
            "acceptance_integrity": True,
            "acceptance_confidentiality": False,
            "unsandboxed_execution_acknowledged": bool(live and allow_unsandboxed),
        },
        "attempts": [],
        "summary": {},
    }
    if profile is not None:
        payload["context_profile"] = {
            "profile_id": profile.profile_id,
            "input_budget_tokens": profile.input_budget_tokens,
            "output_reserve_tokens": profile.output_reserve_tokens,
            "fingerprint": profile.fingerprint,
        }
    results_path = target / "results.json"
    if results_path.is_file():
        existing = json.loads(results_path.read_text(encoding="utf-8"))
        for field in (
            "artifact_schema_version",
            "compare_contexts",
            "repetitions",
            "suite",
            "runtime_identity",
            "configuration_identity",
            "environment_identity",
            "context_profile",
        ):
            if existing.get(field) != payload.get(field):
                raise ValueError(f"cannot resume: local smoke {field} identity changed")
        payload = existing
    else:
        _atomic_write_json(results_path, payload)
    schedule: list[tuple[LocalSmokeTask, int, str | None]] = []
    if compare_contexts:
        for task_index, task in enumerate(suite.tasks):
            for repetition in range(repetitions):
                order = (
                    CONTEXT_VARIANTS
                    if (task_index + repetition) % 2 == 0
                    else tuple(reversed(CONTEXT_VARIANTS))
                )
                schedule.extend((task, repetition, variant) for variant in order)
    else:
        schedule = [
            (task, repetition, None) for repetition in range(repetitions) for task in suite.tasks
        ]
    completed_keys = {
        (
            str(attempt.get("task_id")),
            int(attempt.get("repetition", 0)),
            attempt.get("variant"),
        )
        for attempt in payload["attempts"]
    }
    for task, repetition, variant in schedule:
        key = (task.id, repetition, variant)
        if key in completed_keys:
            continue
        attempt_dir, _ = _local_attempt_paths(target, task.id, repetition, variant)
        metadata_path = attempt_dir / "metadata.json"
        if metadata_path.is_file():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            state = metadata.get("state")
            if state == "generation_frozen":
                attempt = _resume_frozen_local_attempt(
                    suite,
                    task,
                    metadata,
                    output_dir=target,
                    material=suite.material_by_task[task.id],
                )
            elif state == "model_running":
                attempt = _terminalize_interrupted_local_attempt(metadata, metadata_path)
            elif state in {"completed", "agent_error", "benchmark_error"}:
                attempt = metadata
            else:
                raise ValueError(f"cannot resume: unknown attempt state for {task.id}/{repetition}")
            payload["attempts"].append(attempt)
            completed_keys.add(key)
            payload["summary"] = _summarize_attempts(payload["attempts"])
            if compare_contexts:
                payload["comparison"] = build_local_context_comparison(
                    payload["attempts"],
                    suite_id=suite.suite_id,
                    profile_id=profile.profile_id if profile is not None else None,
                    input_budget_tokens=(
                        profile.input_budget_tokens if profile is not None else None
                    ),
                    output_reserve_tokens=(
                        profile.output_reserve_tokens if profile is not None else None
                    ),
                    profile_fingerprint=profile.fingerprint if profile is not None else None,
                    expected_task_ids=tuple(task.id for task in suite.tasks),
                    expected_repetitions=repetitions,
                    provider=base_config.llm.provider,
                    model=base_config.llm.model,
                    formal=bool(live and require_clean_runtime),
                )
            _atomic_write_json(results_path, payload)
            continue
        attempt = _run_attempt(
            suite,
            task,
            repetition=repetition,
            variant=variant,
            output_dir=target,
            base_config=base_config,
            client_factory=factory,
            keep_workspaces=keep_workspaces,
            material=suite.material_by_task[task.id],
            context_profile=profile,
        )
        payload["attempts"].append(attempt)
        completed_keys.add(key)
        payload["summary"] = _summarize_attempts(payload["attempts"])
        if compare_contexts:
            payload["comparison"] = build_local_context_comparison(
                payload["attempts"],
                suite_id=suite.suite_id,
                profile_id=profile.profile_id if profile is not None else None,
                input_budget_tokens=(profile.input_budget_tokens if profile is not None else None),
                output_reserve_tokens=(
                    profile.output_reserve_tokens if profile is not None else None
                ),
                profile_fingerprint=profile.fingerprint if profile is not None else None,
                expected_task_ids=tuple(task.id for task in suite.tasks),
                expected_repetitions=repetitions,
                provider=base_config.llm.provider,
                model=base_config.llm.model,
                formal=bool(live and require_clean_runtime),
            )
        _atomic_write_json(results_path, payload)
    _atomic_write_text(target / "report.md", _render_report(payload))
    return payload


def local_smoke_exit_code(payload: dict[str, Any]) -> int:
    """Map a completed benchmark payload to its documented process exit code."""

    attempts = list(payload.get("attempts") or [])
    if any(attempt.get("execution_status") == "benchmark_error" for attempt in attempts):
        return 1
    if any(
        attempt.get("execution_status") != "completed"
        or attempt.get("verification_status") != "passed"
        for attempt in attempts
    ):
        return 2
    return 0


def _run_attempt(
    suite: LocalSmokeSuite,
    task: LocalSmokeTask,
    *,
    repetition: int,
    variant: str | None,
    output_dir: Path,
    base_config: PaiCliConfig,
    client_factory: ClientFactory,
    keep_workspaces: bool,
    material: FrozenTaskMaterial,
    context_profile: ContextStressProfile | None,
) -> dict[str, Any]:
    started = time.monotonic()
    attempt_dir, run_dir = _local_attempt_paths(output_dir, task.id, repetition, variant)
    attempt_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(
        attempt_dir / "metadata.json",
        {
            "state": "model_running",
            "task_id": task.id,
            "repetition": repetition,
            "variant": variant,
            "pressure_class": task.pressure_class,
            "history_fingerprint": task.history_fingerprint or None,
        },
    )
    acceptance_files = dict(material.acceptance_files)
    workspace: BenchmarkWorkspace | None = None
    response = ""
    events: list[dict[str, Any]] = []
    actual_usage: dict[str, int] | None = None
    context_telemetry: dict[str, Any] = _empty_context_telemetry()
    context_reductions = 0
    context_reduction_actions: list[str] = []
    tool_errors = 0
    turns = 0
    patch = ""
    artifact_patch = ""
    patch_redacted = False
    verifier_output = ""
    execution_status = "completed"
    verification_status = "not_run"
    error: str | None = None
    policy_violations: list[str] = []
    interrupted: KeyboardInterrupt | None = None
    secrets = _configured_secrets(base_config)
    try:
        workspace = materialize_benchmark_workspace(
            task,
            run_dir / "workspace",
            fixture_files=material.fixture_files,
        )
        config = copy.deepcopy(base_config)
        config.policy.audit_log_path = str(run_dir / "audit")
        config.context.tool_result_storage_dir = str(workspace.path / ".paicli" / "tool_results")
        client = client_factory(task, repetition, config)
        secrets.update(_configured_secrets(config))
        agent_result = asyncio.run(
            _execute_production_agent(
                task.prompt,
                workspace.path,
                config,
                client,
                history=task.history,
                context_manager_factory=(
                    full_history_context_manager_factory(context_profile)
                    if variant == "full-history" and context_profile is not None
                    else None
                ),
                snapshot_dir=run_dir / "snapshots",
                secrets=secrets,
            )
        )
        response = agent_result["response"]
        events = agent_result["events"]
        actual_usage = agent_result["actual_usage"]
        context_telemetry = agent_result["context_telemetry"]
        context_reductions = int(agent_result["context_reductions"])
        context_reduction_actions = list(agent_result["context_reduction_actions"])
        tool_errors = int(agent_result["tool_errors"])
        turns = int(agent_result["turns"])
        policy_violations = _benchmark_policy_violations(events)
        if agent_result["error"]:
            execution_status = "agent_error"
            error = str(agent_result["error"])
        if policy_violations:
            execution_status = "agent_error"
            error = "Agent tool policy violation: " + ", ".join(policy_violations)
        patch = collect_benchmark_patch(workspace)
        artifact_patch = patch
        if _contains_sensitive_text(patch, secrets):
            patch_redacted = True
            artifact_patch = ""
            execution_status = "agent_error"
            verification_status = "not_run"
            error = "Agent patch contained a configured credential; patch artifact omitted"
        elif execution_status == "completed":
            safe_response = str(_sanitize_value(response, secrets=secrets))
            safe_events = [_sanitize_value(event, secrets=secrets) for event in events]
            _atomic_write_text(attempt_dir / "patch.diff", artifact_patch)
            _atomic_write_text(attempt_dir / "response.txt", safe_response)
            _atomic_write_jsonl(attempt_dir / "events.jsonl", safe_events)
            _atomic_write_json(
                attempt_dir / "metadata.json",
                {
                    "state": "generation_frozen",
                    "task_id": task.id,
                    "repetition": repetition,
                    "variant": variant,
                    "pressure_class": task.pressure_class,
                    "history_fingerprint": task.history_fingerprint or None,
                    "execution_status": "completed",
                    "verification_status": "not_run",
                    "elapsed_seconds": round(time.monotonic() - started, 6),
                    "turns": turns,
                    "actual_usage": actual_usage,
                    "context_telemetry": context_telemetry,
                    "context_reductions": context_reductions,
                    "context_reduction_actions": context_reduction_actions,
                    "tool_errors": tool_errors,
                    "usage_source": (
                        "synthetic"
                        if actual_usage is not None and client_factory is not _live_client_factory
                        else "provider_reported"
                        if actual_usage is not None
                        else None
                    ),
                    "patch_bytes": len(artifact_patch.encode("utf-8")),
                    "raw_patch_bytes": len(patch.encode("utf-8")),
                    "patch_redacted": False,
                    "patch_path": (attempt_dir / "patch.diff").relative_to(output_dir).as_posix(),
                    "events_path": (attempt_dir / "events.jsonl")
                    .relative_to(output_dir)
                    .as_posix(),
                    "verifier_log_path": (attempt_dir / "verifier.log")
                    .relative_to(output_dir)
                    .as_posix(),
                    "error": None,
                    "policy_violations": [],
                    "workspace_path": None,
                },
            )
            verification = _verify_attempt(
                suite,
                task,
                patch,
                acceptance_files,
                run_dir / "verifier",
                fixture_files=material.fixture_files,
            )
            verification_status = verification["status"]
            verifier_output = verification["output"]
            if verification["error"]:
                execution_status = "benchmark_error"
                error = verification["error"]
    except KeyboardInterrupt as exc:
        execution_status = "agent_error"
        verification_status = "not_run"
        error = "KeyboardInterrupt"
        interrupted = exc
    except Exception as exc:  # noqa: BLE001 - one broken attempt must not stop the suite
        execution_status = "benchmark_error"
        verification_status = "not_run"
        error = f"{type(exc).__name__}: {exc}"
    safe_response = str(_sanitize_value(response, secrets=secrets))
    safe_verifier_output = str(_sanitize_value(verifier_output, secrets=secrets))
    safe_events = [_sanitize_value(event, secrets=secrets) for event in events]
    _atomic_write_text(attempt_dir / "patch.diff", artifact_patch)
    _atomic_write_text(attempt_dir / "response.txt", safe_response)
    _atomic_write_jsonl(attempt_dir / "events.jsonl", safe_events)
    _atomic_write_text(attempt_dir / "verifier.log", safe_verifier_output)

    error = str(_sanitize_value(error, secrets=secrets)) if error is not None else None
    retain_workspace = keep_workspaces and not patch_redacted and interrupted is None
    row = {
        "state": execution_status,
        "task_id": task.id,
        "repetition": repetition,
        "variant": variant,
        "pressure_class": task.pressure_class,
        "history_fingerprint": task.history_fingerprint or None,
        "execution_status": execution_status,
        "verification_status": verification_status,
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "turns": turns,
        "actual_usage": actual_usage,
        "context_telemetry": context_telemetry,
        "context_reductions": context_reductions,
        "context_reduction_actions": context_reduction_actions,
        "tool_errors": tool_errors,
        "usage_source": (
            "synthetic"
            if actual_usage is not None and client_factory is not _live_client_factory
            else "provider_reported"
            if actual_usage is not None
            else None
        ),
        "patch_bytes": len(artifact_patch.encode("utf-8")),
        "raw_patch_bytes": len(patch.encode("utf-8")),
        "patch_redacted": patch_redacted,
        "patch_path": (attempt_dir / "patch.diff").relative_to(output_dir).as_posix(),
        "events_path": (attempt_dir / "events.jsonl").relative_to(output_dir).as_posix(),
        "verifier_log_path": (attempt_dir / "verifier.log").relative_to(output_dir).as_posix(),
        "error": error,
        "policy_violations": policy_violations,
        "workspace_path": (
            str(workspace.path) if retain_workspace and workspace is not None else None
        ),
    }
    _atomic_write_json(attempt_dir / "metadata.json", row)
    if retain_workspace:
        for transient in ("audit", "snapshots", "verifier"):
            transient_path = run_dir / transient
            if transient_path.exists():
                _remove_tree(transient_path)
    else:
        _remove_tree(run_dir)
    if interrupted is not None:
        raise interrupted
    return row


def _local_attempt_paths(
    output_dir: Path,
    task_id: str,
    repetition: int,
    variant: str | None,
) -> tuple[Path, Path]:
    parts = (task_id, variant, str(repetition)) if variant else (task_id, str(repetition))
    relative = Path(*parts)
    return output_dir / "attempts" / relative, output_dir / "runs" / relative


def _resume_frozen_local_attempt(
    suite: LocalSmokeSuite,
    task: LocalSmokeTask,
    metadata: dict[str, Any],
    *,
    output_dir: Path,
    material: FrozenTaskMaterial,
) -> dict[str, Any]:
    variant = metadata.get("variant")
    repetition = int(metadata["repetition"])
    attempt_dir, run_dir = _local_attempt_paths(output_dir, task.id, repetition, variant)
    patch_path = output_dir / str(metadata["patch_path"])
    patch = patch_path.read_text(encoding="utf-8")
    verifier_target = run_dir / "verifier"
    if verifier_target.exists():
        _remove_tree(verifier_target)
    verification = _verify_attempt(
        suite,
        task,
        patch,
        dict(material.acceptance_files),
        verifier_target,
        fixture_files=dict(material.fixture_files),
    )
    row = dict(metadata)
    row["verification_status"] = verification["status"]
    row["state"] = "benchmark_error" if verification["error"] else "completed"
    row["execution_status"] = row["state"]
    row["error"] = verification["error"]
    _atomic_write_text(
        output_dir / str(metadata["verifier_log_path"]),
        str(_sanitize_value(verification["output"])),
    )
    _atomic_write_json(attempt_dir / "metadata.json", row)
    if run_dir.exists():
        _remove_tree(run_dir)
    return row


def _terminalize_interrupted_local_attempt(
    metadata: dict[str, Any],
    metadata_path: Path,
) -> dict[str, Any]:
    row = {
        **metadata,
        "state": "agent_error",
        "execution_status": "agent_error",
        "verification_status": "not_run",
        "elapsed_seconds": 0.0,
        "turns": 0,
        "actual_usage": None,
        "context_telemetry": _empty_context_telemetry(),
        "context_reductions": 0,
        "context_reduction_actions": [],
        "tool_errors": 0,
        "usage_source": None,
        "patch_bytes": 0,
        "raw_patch_bytes": 0,
        "patch_redacted": False,
        "patch_path": "",
        "events_path": "",
        "verifier_log_path": "",
        "error": "interrupted after model execution started; attempt was not resampled",
        "policy_violations": [],
        "workspace_path": None,
    }
    _atomic_write_json(metadata_path, row)
    return row


async def _execute_production_agent(
    prompt: str,
    workspace: Path,
    config: PaiCliConfig,
    client: Any,
    *,
    history: tuple[Message, ...] = (),
    context_manager_factory: Callable[..., ContextManager] | None = None,
    snapshot_dir: Path,
    secrets: set[str],
) -> dict[str, Any]:
    registry = _benchmark_tool_registry()
    engine = QueryEngine(
        llm_client=client,
        tool_registry=registry,
        config=config,
        cwd=str(workspace),
        context_manager_factory=context_manager_factory,
    )
    response = ""
    events: list[dict[str, Any]] = []
    input_tokens = 0
    output_tokens = 0
    usage_seen = False
    context_telemetry = _empty_context_telemetry()
    context_reductions = 0
    context_reduction_actions: list[str] = []
    tool_errors = 0
    turns = 0
    error: str | None = None
    with _temporary_environment(
        {
            "PAICLI_SNAPSHOT_DIR": str(snapshot_dir),
            "PAICLI_SNAPSHOT_ENABLED": "true",
        },
        remove=_secret_environment_names(),
    ):
        try:
            async for event in engine.ask(prompt, history=list(history)):
                event_type = str(event.get("type") or "")
                if event_type == "text_delta":
                    response += str(event.get("text") or "")
                elif event_type == "usage":
                    usage = dict(event.get("usage") or {})
                    input_tokens += int(usage.get("input_tokens") or 0)
                    output_tokens += int(usage.get("output_tokens") or 0)
                    usage_seen = True
                elif event_type == "context_usage":
                    source = (
                        "estimated" if bool(event.get("estimated", True)) else "provider_reported"
                    )
                    bucket = context_telemetry[source]
                    bucket["samples"] = int(bucket["samples"] or 0) + 1
                    bucket["max_used_tokens"] = max(
                        int(bucket["max_used_tokens"] or 0),
                        int(event.get("used_tokens") or 0),
                    )
                    bucket["max_pressure_ratio"] = max(
                        float(bucket["max_pressure_ratio"] or 0.0),
                        float(event.get("pressure_ratio") or 0.0),
                    )
                elif event_type == "context_reduced":
                    context_reductions += 1
                    for action in event.get("actions") or []:
                        value = str(action)
                        if value not in context_reduction_actions:
                            context_reduction_actions.append(value)
                elif event_type == "tool_result" and bool(event.get("is_error")):
                    tool_errors += 1
                elif event_type == "done":
                    turns = int(event.get("total_turns") or 0)
                elif event_type == "error":
                    value = event.get("error")
                    error = f"{type(value).__name__}: {value}"
                sanitized = _sanitize_event(event, secrets=secrets)
                if sanitized is not None:
                    events.append(sanitized)
        except Exception as exc:  # noqa: BLE001 - provider boundary becomes attempt data
            error = f"{type(exc).__name__}: {exc}"
    actual_usage = (
        {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
        if usage_seen
        else None
    )
    return {
        "response": response,
        "events": events,
        "actual_usage": actual_usage,
        "context_telemetry": context_telemetry,
        "context_reductions": context_reductions,
        "context_reduction_actions": context_reduction_actions,
        "tool_errors": tool_errors,
        "turns": turns,
        "error": error,
    }


def _benchmark_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in get_builtin_tools():
        if tool.name not in TOOL_PROFILE:
            continue
        if tool.name == "execute_command":
            original_handler = tool.handler

            async def restricted_command(payload, context, *, handler=original_handler):
                command = str(payload.get("command") or "")
                if not _benchmark_command_allowed(command):
                    return ToolResult(
                        content=(
                            "Command rejected by the local-smoke read-only/verification "
                            "shell profile."
                        ),
                        is_error=True,
                        error_kind="benchmark_policy",
                    )
                return await handler(payload, context)

            tool = replace(
                tool,
                description=(
                    f"{tool.description} Local-smoke permits only one read-only or "
                    "verification command with no chaining."
                ),
                handler=restricted_command,
            )
        registry.register(tool)
    return registry


def _configure_benchmark(
    config: PaiCliConfig,
    output_dir: Path,
    *,
    context_profile: ContextStressProfile | None = None,
) -> None:
    config.llm.temperature = 0.0
    config.agent.max_turns = 20
    config.agent.max_tool_calls = 40
    config.agent.max_elapsed_seconds = 600.0
    config.agent.max_total_tokens = 100_000
    config.policy.hitl_mode = "never"
    config.policy.require_approval_for_writes = False
    config.policy.audit_log_path = str(output_dir / "audit")
    config.features.mcp = False
    config.features.skill = False
    config.features.memory = False
    config.memory.long_term_enabled = False
    if context_profile is not None:
        config.context.output_reserve_tokens = context_profile.output_reserve_tokens
        fixed_chars = context_profile.input_budget_tokens * 4
        config.context.min_budget_chars = fixed_chars
        config.context.max_budget_chars = fixed_chars
    config.tools.enabled = sorted(TOOL_PROFILE)
    config.tools.disabled = sorted(
        tool.name for tool in get_builtin_tools() if tool.name not in TOOL_PROFILE
    )


def _live_client_factory(_task: LocalSmokeTask, _repetition: int, config: PaiCliConfig) -> Any:
    return create_llm_client(
        config.llm,
        retry_policy=config.retry.resolve("llm"),
        retry_audit_path=config.policy.audit_log_path,
        retry_cwd=str(_runtime_root()),
    )


def _verify_attempt(
    suite: LocalSmokeSuite,
    task: LocalSmokeTask,
    patch: str,
    acceptance_files: dict[str, bytes],
    target: Path,
    *,
    fixture_files: dict[str, bytes],
) -> dict[str, Any]:
    workspace = materialize_benchmark_workspace(
        task,
        target,
        fixture_files=fixture_files,
    )
    if patch:
        applied = subprocess.run(
            ["git", "apply", "--binary", "-"],
            cwd=workspace.path,
            input=patch.encode("utf-8"),
            capture_output=True,
            check=False,
        )
        if applied.returncode != 0:
            return {
                "status": "not_run",
                "output": (applied.stdout + applied.stderr).decode("utf-8", errors="replace"),
                "error": f"benchmark patch failed to apply ({applied.returncode})",
            }
    for relative, content in acceptance_files.items():
        destination = workspace.path / Path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    env = {
        key: value for key, value in os.environ.items() if key not in _secret_environment_names()
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    existing_path = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(workspace.path)
        if not existing_path
        else os.pathsep.join((str(workspace.path), existing_path))
    )
    try:
        process = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=workspace.path,
            env=env,
            text=True,
            capture_output=True,
            timeout=suite.verifier_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = _decode_subprocess_output(exc.stdout) + _decode_subprocess_output(exc.stderr)
        return {
            "status": "not_run",
            "output": output,
            "error": f"acceptance verifier timed out after {suite.verifier_timeout_seconds}s",
        }
    return {
        "status": "passed" if process.returncode == 0 else "failed",
        "output": process.stdout + process.stderr,
        "error": None,
    }


def _empty_context_telemetry() -> dict[str, dict[str, int | float | None]]:
    return {
        source: {
            "max_used_tokens": None,
            "max_pressure_ratio": None,
            "samples": 0,
        }
        for source in ("estimated", "provider_reported")
    }


def _configured_secrets(config: PaiCliConfig) -> set[str]:
    return {value for value in (config.llm.api_key,) if value}


def _benchmark_policy_violations(events: list[dict[str, Any]]) -> list[str]:
    violations: list[str] = []
    network_or_install = re.compile(
        r"(?i)(?:"
        r"\bpip(?:3)?\s+install\b|"
        r"\bpython(?:3)?\s+-m\s+pip\s+install\b|"
        r"\buv\s+(?:add|sync|pip\s+install)\b|"
        r"\bnpm\s+(?:install|ci)\b|"
        r"\b(?:curl|wget|invoke-webrequest|iwr|ssh|scp|nc|ncat|telnet)\b|"
        r"\bgit\s+(?:clone|fetch|pull|push)\b|"
        r"\b(?:urllib\.request|requests\.(?:get|post|put|patch|delete)|"
        r"httpx\.(?:get|post|put|patch|delete)|socket\.create_connection)\b"
        r")"
    )
    for event in events:
        if event.get("type") != "tool_call":
            continue
        value = json.dumps(event.get("input") or {}, ensure_ascii=False)
        normalized = value.replace("\\\\", "/").replace("\\", "/").lower()
        if "acceptance/" in normalized and "acceptance_access" not in violations:
            violations.append("acceptance_access")
        if (
            event.get("name") == "execute_command"
            and network_or_install.search(str((event.get("input") or {}).get("command") or ""))
            and "network_or_dependency_install" not in violations
        ):
            violations.append("network_or_dependency_install")
        if (
            event.get("name") == "execute_command"
            and not _benchmark_command_allowed(str((event.get("input") or {}).get("command") or ""))
            and "shell_command_outside_profile" not in violations
        ):
            violations.append("shell_command_outside_profile")
    return violations


def _benchmark_command_allowed(command: str) -> bool:
    normalized = command.strip()
    if not normalized or re.search(r"[\r\n;&|`]|\$\(", normalized):
        return False
    if re.search(
        r"(?i)(?:^|\s)(?:--pre(?:=|\s)|--hostname-bin\b|--ext-diff\b|--textconv\b)",
        normalized,
    ):
        return False
    allowed = re.compile(
        r"(?i)^(?:"
        r"python(?:3)?\s+-m\s+(?:pytest|unittest|compileall)\b|"
        r"pytest\b|"
        r"git\s+(?:status|diff|grep|show|log|rev-parse)\b|"
        r"rg\b|"
        r"(?:get-content|get-childitem|select-string|test-path)\b|"
        r"(?:dir|ls|pwd|type|findstr)\b"
        r")"
    )
    return bool(allowed.match(normalized))


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


def _sanitize_event(event: dict[str, Any], *, secrets: set[str]) -> dict[str, Any] | None:
    if event.get("type") == "thinking_delta":
        return None
    without_private_state = {
        key: value
        for key, value in event.items()
        if key not in {"messages", "thinking", "reasoning_content"}
    }
    return _sanitize_value(without_private_state, secrets=secrets)


def _decode_subprocess_output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _sanitize_value(value: Any, *, key: str = "", secrets: set[str] | None = None) -> Any:
    known_secrets = secrets or set()
    lowered = key.lower()
    secret_key = lowered in {"api_key", "authorization", "password", "secret"} or lowered.endswith(
        "_api_key"
    )
    if secret_key:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize_value(
                item,
                key=str(item_key),
                secrets=known_secrets,
            )
            for item_key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_sanitize_value(item, secrets=known_secrets) for item in value]
    if isinstance(value, BaseException):
        return _sanitize_value(
            f"{type(value).__name__}: {value}",
            secrets=known_secrets,
        )
    if isinstance(value, str):
        return _redact_sensitive_text(value, known_secrets)[:2000]
    if value is None or isinstance(value, bool | int | float):
        return value
    return str(value)[:2000]


def _summarize_attempts(attempts: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "scheduled_attempts": len(attempts),
        "passed_attempts": sum(item["verification_status"] == "passed" for item in attempts),
        "failed_attempts": sum(item["verification_status"] == "failed" for item in attempts),
        "agent_errors": sum(item["execution_status"] == "agent_error" for item in attempts),
        "benchmark_errors": sum(item["execution_status"] == "benchmark_error" for item in attempts),
    }


def build_local_context_comparison(
    attempts: list[dict[str, Any]],
    *,
    suite_id: str | None = None,
    profile_id: str | None = None,
    input_budget_tokens: int | None = None,
    output_reserve_tokens: int | None = None,
    profile_fingerprint: str | None = None,
    expected_task_ids: tuple[str, ...] | None = None,
    expected_repetitions: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    formal: bool = False,
) -> dict[str, Any]:
    """Build the paired local context-strategy comparison from scheduled attempts."""

    variants: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[dict[str, Any]]] = {
        variant: [attempt for attempt in attempts if attempt.get("variant") == variant]
        for variant in CONTEXT_VARIANTS
    }
    for variant, rows in grouped.items():
        passed = sum(row.get("verification_status") == "passed" for row in rows)
        input_values: list[int] = []
        usage_complete = True
        for row in rows:
            usage = row.get("actual_usage")
            value = usage.get("input_tokens") if isinstance(usage, dict) else None
            if (
                row.get("usage_source") != "provider_reported"
                or not isinstance(value, int)
                or isinstance(value, bool)
            ):
                usage_complete = False
            else:
                input_values.append(value)
        scheduled = len(rows)
        variants[variant] = {
            "scheduled": scheduled,
            "passed": passed,
            "empirical_pass_at_1": passed / scheduled if scheduled else 0.0,
            "usage_complete": usage_complete and len(input_values) == scheduled,
            "average_provider_input_tokens": (
                sum(input_values) / scheduled
                if scheduled and usage_complete and len(input_values) == scheduled
                else None
            ),
        }

    optimized_by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in grouped["optimized"]:
        optimized_by_task[str(row.get("task_id"))].append(row)
    pressure_tasks_with_reduction = sum(
        any(row.get("context_reduction_actions") for row in rows)
        for rows in optimized_by_task.values()
        if rows and rows[0].get("pressure_class") in {"medium", "high"}
    )
    summary_actions = {"history_summary", "history_deterministic"}
    high_tasks_with_summary = sum(
        any(
            summary_actions.intersection(row.get("context_reduction_actions") or []) for row in rows
        )
        for rows in optimized_by_task.values()
        if rows and rows[0].get("pressure_class") == "high"
    )
    pressure_coverage = {
        "pressure_tasks_with_reduction": pressure_tasks_with_reduction,
        "required_pressure_tasks_with_reduction": 4,
        "high_tasks_with_summary": high_tasks_with_summary,
        "required_high_tasks_with_summary": 2,
        "eligible": (pressure_tasks_with_reduction >= 4 and high_tasks_with_summary >= 2),
    }

    baseline = variants["full-history"]
    optimized = variants["optimized"]
    baseline_average = baseline["average_provider_input_tokens"]
    optimized_average = optimized["average_provider_input_tokens"]
    reduction = (
        (baseline_average - optimized_average) / baseline_average
        if isinstance(baseline_average, int | float)
        and baseline_average > 0
        and isinstance(optimized_average, int | float)
        else None
    )
    infrastructure_complete = not any(
        row.get("execution_status") == "benchmark_error" for row in attempts
    )
    expected_keys = {
        (task_id, repetition, variant)
        for task_id in (expected_task_ids or ())
        for repetition in range(expected_repetitions or 0)
        for variant in CONTEXT_VARIANTS
    }
    observed_key_counts: dict[tuple[str, int, str], int] = defaultdict(int)
    for row in attempts:
        variant = row.get("variant")
        if variant in CONTEXT_VARIANTS:
            observed_key_counts[
                (
                    str(row.get("task_id")),
                    int(row.get("repetition", 0)),
                    str(variant),
                )
            ] += 1
    pairing_complete = bool(
        set(expected_task_ids or ()) == LOCAL_SMOKE_V2_TASK_IDS
        and expected_repetitions == 3
        and set(observed_key_counts) == expected_keys
        and all(count == 1 for count in observed_key_counts.values())
    )
    formal_identity_complete = bool(
        formal
        and suite_id == "local-smoke-v2"
        and profile_id == "stress-16k-v1"
        and input_budget_tokens == 16_384
        and output_reserve_tokens == 4_096
        and profile_fingerprint == STRESS_16K_V1_FINGERPRINT
        and provider == "qwen"
        and model == "qwen3.7-plus"
        and pairing_complete
    )
    claim_eligible = bool(
        formal_identity_complete
        and baseline["scheduled"] == 21
        and optimized["scheduled"] == 21
        and baseline["usage_complete"]
        and optimized["usage_complete"]
        and infrastructure_complete
        and optimized["empirical_pass_at_1"] > baseline["empirical_pass_at_1"]
        and reduction is not None
        and reduction > 0
        and pressure_coverage["eligible"]
    )
    statement = None
    if claim_eligible:
        statement = (
            "在固定 local-smoke-v2 任务套件上，"
            f"将 PaiCLI pass@1 从 {baseline['empirical_pass_at_1']:.1%} "
            f"提升至 {optimized['empirical_pass_at_1']:.1%}，"
            f"平均 token 消耗降低 {reduction:.1%}。"
        )

    pairs: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in attempts:
        variant = row.get("variant")
        if variant in CONTEXT_VARIANTS:
            pairs[(str(row.get("task_id")), int(row.get("repetition", 0)))][str(variant)] = row
    return {
        "variants": variants,
        "empirical_pass_at_1_change_points": (
            optimized["empirical_pass_at_1"] - baseline["empirical_pass_at_1"]
        )
        * 100,
        "input_token_reduction": reduction,
        "pressure_coverage": pressure_coverage,
        "infrastructure_complete": infrastructure_complete,
        "pairing_complete": pairing_complete,
        "paired_attempts": [
            {
                "task_id": key[0],
                "repetition": key[1],
                "full-history": value.get("full-history"),
                "optimized": value.get("optimized"),
            }
            for key, value in sorted(pairs.items())
        ],
        "claim_eligible": claim_eligible,
        "formal_identity_complete": formal_identity_complete,
        "suggested_statement": statement,
    }


def _render_report(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        f"# {payload['suite']['id']} Report",
        "",
        f"Suite fingerprint: `{payload['suite']['fingerprint']}`",
        "",
        (
            f"Passed **{summary.get('passed_attempts', 0)}/"
            f"{summary.get('scheduled_attempts', 0)}** scheduled attempts."
        ),
        "",
    ]
    comparison = payload.get("comparison")
    if isinstance(comparison, dict):
        variants = comparison["variants"]
        lines.extend(
            [
                "## Context Comparison",
                "",
                "| Variant | Passed | Empirical pass@1 | Avg provider input tokens |",
                "| --- | ---: | ---: | ---: |",
            ]
        )
        for variant in CONTEXT_VARIANTS:
            item = variants[variant]
            average = item["average_provider_input_tokens"]
            average_text = f"{average:.1f}" if isinstance(average, int | float) else "n/a"
            lines.append(
                f"| `{variant}` | {item['passed']}/{item['scheduled']} | "
                f"{item['empirical_pass_at_1']:.1%} | {average_text} |"
            )
        reduction = comparison.get("input_token_reduction")
        reduction_text = f"{reduction:.1%}" if isinstance(reduction, int | float) else "n/a"
        coverage = comparison["pressure_coverage"]
        lines.extend(
            [
                "",
                (
                    f"- Pass@1 change: "
                    f"{comparison['empirical_pass_at_1_change_points']:+.1f} percentage points"
                ),
                f"- Input-token reduction: {reduction_text}",
                (
                    "- Pressure coverage: "
                    f"{coverage['pressure_tasks_with_reduction']} pressure tasks reduced; "
                    f"{coverage['high_tasks_with_summary']} high-pressure tasks summarized"
                ),
                f"- Claim eligible: {str(comparison['claim_eligible']).lower()}",
                "",
                (comparison["suggested_statement"] or "不满足自动生成改进表述的证据门槛。"),
                "",
            ]
        )
    has_variants = any(attempt.get("variant") for attempt in payload["attempts"])
    variant_column = " Variant |" if has_variants else ""
    variant_rule = " --- |" if has_variants else ""
    lines.extend(
        [
            f"| Task | Repetition |{variant_column} Execution | Verification | Tokens | Seconds |",
            f"| --- | ---: |{variant_rule} --- | --- | ---: | ---: |",
        ]
    )
    for attempt in payload["attempts"]:
        usage = attempt.get("actual_usage") or {}
        token_text = usage.get("total_tokens", "n/a")
        variant_value = f" `{attempt.get('variant')}` |" if has_variants else ""
        lines.append(
            f"| `{attempt['task_id']}` | {attempt['repetition']} |{variant_value} "
            f"{attempt['execution_status']} | {attempt['verification_status']} | "
            f"{token_text} | {attempt['elapsed_seconds']} |"
        )
    lines.extend(
        [
            "",
            (
                "> This benchmark is not filesystem- or network-isolated. "
                "Acceptance integrity is guaranteed; acceptance confidentiality is not."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _configuration_identity(config: PaiCliConfig) -> dict[str, Any]:
    details = {
        "provider": config.llm.provider,
        "model": config.llm.model,
        "base_url_hash": _hash_text(config.llm.base_url or ""),
        "temperature": config.llm.temperature,
        "max_tokens": config.llm.max_tokens,
        "context_window": config.llm.context_window,
        "tool_profile": TOOL_PROFILE_NAME,
        "tools": sorted(TOOL_PROFILE),
        "agent_budget": {
            "max_turns": config.agent.max_turns,
            "max_tool_calls": config.agent.max_tool_calls,
            "max_elapsed_seconds": config.agent.max_elapsed_seconds,
            "max_total_tokens": config.agent.max_total_tokens,
        },
    }
    return {**details, "fingerprint": _hash_json(details)}


def _environment_identity() -> dict[str, Any]:
    details = {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "pytest": importlib.metadata.version("pytest"),
    }
    return {**details, "fingerprint": _hash_json(details)}


def _runtime_identity() -> dict[str, Any]:
    root = _runtime_root()
    revision: str | None = None
    dirty = False
    if (root / ".git").exists():
        revision_result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=False
        )
        if revision_result.returncode == 0:
            revision = revision_result.stdout.strip()
        dirty_result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        dirty = bool(dirty_result.stdout.strip())
    source_root = root / "src" / "paicli"
    if not source_root.is_dir():
        source_root = Path(__file__).resolve().parents[1]
    fingerprint = _hash_file_tree(source_root)
    return {
        "version": __version__,
        "revision": revision,
        "dirty": dirty,
        "fingerprint": fingerprint,
    }


def _runtime_root() -> Path:
    candidate = Path(__file__).resolve()
    for parent in candidate.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return candidate.parents[2]


def _hash_file_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _hash_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@contextlib.contextmanager
def _local_smoke_lock(output_dir: Path):
    lock_path = output_dir / ".local-smoke.lock"
    owner = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "created_at_unix_ns": time.time_ns(),
    }
    while True:
        try:
            with lock_path.open("x", encoding="utf-8") as handle:
                json.dump(owner, handle, sort_keys=True)
            break
        except FileExistsError:
            try:
                existing = json.loads(lock_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                existing = {}
            if not existing:
                with contextlib.suppress(OSError):
                    if time.time() - lock_path.stat().st_mtime < 30:
                        raise RuntimeError(
                            "local smoke lock is currently being initialized"
                        ) from None
            existing_pid = existing.get("pid")
            existing_host = existing.get("hostname")
            if (
                isinstance(existing_pid, int)
                and existing_pid > 0
                and isinstance(existing_host, str)
                and (existing_host != socket.gethostname() or _process_is_running(existing_pid))
            ):
                raise RuntimeError(
                    "local smoke evaluation is already active for this output directory "
                    f"(pid={existing_pid}, host={existing_host})"
                ) from None
            with contextlib.suppress(FileNotFoundError):
                lock_path.unlink()
    try:
        yield
    finally:
        with contextlib.suppress(OSError, ValueError, json.JSONDecodeError):
            if json.loads(lock_path.read_text(encoding="utf-8")) == owner:
                lock_path.unlink()


def _process_is_running(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _atomic_write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    _atomic_write_text(path, text)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temporary.write(text)
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def _remove_tree(path: Path) -> None:
    def make_writable_and_retry(
        function: Callable[[str], object], failed_path: str, _error: object
    ) -> None:
        os.chmod(failed_path, stat.S_IWRITE)
        function(failed_path)

    shutil.rmtree(path, onerror=make_writable_and_retry)


def _secret_environment_names() -> set[str]:
    markers = (
        "API_KEY",
        "ACCESS_KEY",
        "AUTHORIZATION",
        "CREDENTIAL",
        "PASSWORD",
        "PRIVATE_KEY",
        "SECRET",
    )
    return {
        key
        for key in os.environ
        if any(marker in key.upper() for marker in markers)
        or key.upper() == "TOKEN"
        or key.upper().endswith("_TOKEN")
    }


@contextlib.contextmanager
def _temporary_environment(values: dict[str, str], *, remove: Iterable[str] = ()) -> Iterator[None]:
    affected = set(values) | set(remove)
    previous = {key: os.environ.get(key) for key in affected}
    for key in remove:
        os.environ.pop(key, None)
    os.environ.update(values)
    try:
        yield
    finally:
        for key, prior in previous.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior


def _run_git(
    cwd: Path, *arguments: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def _resolve_suite_directory(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    if target == root or root not in target.parents:
        raise ValueError(f"benchmark paths must resolve inside the suite root: {relative}")
    if not target.is_dir():
        raise ValueError(f"benchmark directory does not exist: {relative}")
    return target


def _resolve_suite_file(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    if root not in target.parents:
        raise ValueError(f"benchmark paths must resolve inside the suite root: {relative}")
    if not target.is_file():
        raise ValueError(f"benchmark file does not exist: {relative}")
    return target


def _load_task_history(
    root: Path,
    relative: str,
    task_id: str,
) -> tuple[tuple[Message, ...], str]:
    path = _resolve_suite_file(root, relative)
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != 1
        or data.get("task_id") != task_id
        or not isinstance(data.get("messages"), list)
    ):
        raise ValueError(f"invalid structured history for task: {task_id}")
    messages: list[Message] = []
    pending_tool_calls: set[str] = set()
    for index, item in enumerate(data["messages"]):
        if not isinstance(item, dict) or set(item) - {
            "role",
            "content",
            "tool_call_id",
            "tool_calls",
        }:
            raise ValueError(f"invalid history message {index} for task: {task_id}")
        role = item.get("role")
        content = item.get("content")
        if role not in {"user", "assistant", "tool"} or not isinstance(content, str):
            raise ValueError(f"invalid history message {index} for task: {task_id}")
        normalized_content = content.replace("\\", "/").lower()
        if "acceptance/" in normalized_content:
            raise ValueError(f"history references acceptance material for task: {task_id}")
        tool_call_id = item.get("tool_call_id")
        tool_calls = item.get("tool_calls", [])
        if tool_call_id is not None and not isinstance(tool_call_id, str):
            raise ValueError(f"invalid history message {index} for task: {task_id}")
        if not isinstance(tool_calls, list):
            raise ValueError(f"invalid history message {index} for task: {task_id}")
        if role != "assistant" and tool_calls:
            raise ValueError(f"invalid history message {index} for task: {task_id}")
        for call in tool_calls:
            if not isinstance(call, dict):
                raise ValueError(f"invalid history tool call for task: {task_id}")
            call_id = call.get("id")
            function = call.get("function")
            if (
                not isinstance(call_id, str)
                or not call_id
                or call_id in pending_tool_calls
                or not isinstance(function, dict)
                or function.get("name") not in TOOL_PROFILE
                or not isinstance(function.get("arguments"), str)
            ):
                raise ValueError(f"invalid history tool call for task: {task_id}")
            try:
                arguments = json.loads(function["arguments"])
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid history tool call for task: {task_id}") from exc
            if not isinstance(arguments, dict):
                raise ValueError(f"invalid history tool call for task: {task_id}")
            normalized_arguments = json.dumps(arguments, ensure_ascii=False).replace("\\", "/")
            if "acceptance/" in normalized_arguments.lower():
                raise ValueError(f"history references acceptance material for task: {task_id}")
            pending_tool_calls.add(call_id)
        if role == "tool":
            if not tool_call_id or tool_call_id not in pending_tool_calls:
                raise ValueError(
                    f"history tool result has no matching tool call for task: {task_id}"
                )
            pending_tool_calls.remove(tool_call_id)
        elif tool_call_id is not None:
            raise ValueError(f"invalid history message {index} for task: {task_id}")
        messages.append(
            Message(
                role=role,
                content=content,
                tool_call_id=tool_call_id,
                tool_calls=tool_calls,
            )
        )
    if pending_tool_calls:
        raise ValueError(f"history tool call has no matching result for task: {task_id}")
    return tuple(messages), hashlib.sha256(raw).hexdigest()


def _read_material_tree(directory: Path, *, label: str) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if ".git" in relative.parts:
            raise ValueError(f"{label} material cannot contain Git metadata: {path}")
        if path.is_symlink():
            raise ValueError(f"{label} material cannot contain symlinks: {path}")
        if _is_transient_material(relative) or not path.is_file():
            continue
        files[relative.as_posix()] = path.read_bytes()
    if not files:
        raise ValueError(f"{label} material is empty: {directory}")
    return files


def _is_transient_material(relative: Path) -> bool:
    return any(
        part in {".paicli", ".pytest_cache", "__pycache__"} for part in relative.parts
    ) or relative.suffix in {".pyc", ".pyo"}


def _content_fingerprint(
    manifest: dict[str, Any],
    tasks: tuple[LocalSmokeTask, ...],
    material_by_task: dict[str, FrozenTaskMaterial],
) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    for task in tasks:
        material = material_by_task[task.id]
        digest.update(f"\0{task.id}\0history\0".encode())
        digest.update(task.history_fingerprint.encode())
        for label, files in (
            ("fixture", material.fixture_files),
            ("acceptance", material.acceptance_files),
        ):
            for relative, content in sorted(files.items()):
                digest.update(f"\0{task.id}\0{label}\0{relative}\0".encode())
                digest.update(content)
    return digest.hexdigest()


__all__ = [
    "BenchmarkWorkspace",
    "LocalSmokeSuite",
    "LocalSmokeTask",
    "collect_benchmark_patch",
    "build_local_context_comparison",
    "load_local_smoke_suite",
    "local_smoke_exit_code",
    "materialize_benchmark_workspace",
    "run_local_smoke",
]
