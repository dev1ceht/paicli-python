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
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from paicli import __version__
from paicli.agent import QueryEngine
from paicli.config import PaiCliConfig, load_config
from paicli.llm import create_llm_client
from paicli.tools import ToolRegistry, get_builtin_tools

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


@dataclass(frozen=True, slots=True)
class LocalSmokeTask:
    id: str
    prompt: str
    fixture_repo: Path
    acceptance: Path
    provenance: dict[str, str] = field(default_factory=dict)


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
        "schema_version": {"const": 1},
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
    task_ids = [str(item["id"]) for item in data["tasks"]]
    seen: set[str] = set()
    for task_id in task_ids:
        if task_id in seen:
            raise ValueError(f"duplicate task id: {task_id}")
        seen.add(task_id)
    tasks = tuple(
        LocalSmokeTask(
            id=str(item["id"]),
            prompt=str(item["prompt"]),
            fixture_repo=_resolve_suite_directory(root, str(item["fixture_repo"])),
            acceptance=_resolve_suite_directory(root, str(item["acceptance"])),
            provenance={str(key): str(value) for key, value in item.get("provenance", {}).items()},
        )
        for item in data["tasks"]
    )
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
) -> dict[str, Any]:
    """Run a local smoke suite serially and persist reconstructable artifacts."""

    if repetitions < 1:
        raise ValueError("repetitions must be at least one")
    live = client_factory is None
    if live and not allow_unsandboxed:
        raise ValueError("live benchmark execution requires --allow-unsandboxed")

    suite = load_local_smoke_suite(manifest_path)
    target = Path(output_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    runtime_identity = _runtime_identity()
    if require_clean_runtime and runtime_identity["dirty"]:
        raise ValueError("--require-clean-runtime rejects the dirty PaiCLI runtime")

    base_config = load_config(project_root=_runtime_root())
    _configure_benchmark(base_config, target)
    factory = client_factory or _live_client_factory
    payload: dict[str, Any] = {
        "artifact_schema_version": 1,
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
    for repetition in range(repetitions):
        for task in suite.tasks:
            attempt = _run_attempt(
                suite,
                task,
                repetition=repetition,
                output_dir=target,
                base_config=base_config,
                client_factory=factory,
                keep_workspaces=keep_workspaces,
                material=suite.material_by_task[task.id],
            )
            payload["attempts"].append(attempt)
            payload["summary"] = _summarize_attempts(payload["attempts"])
            _atomic_write_json(target / "results.json", payload)
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
    output_dir: Path,
    base_config: PaiCliConfig,
    client_factory: ClientFactory,
    keep_workspaces: bool,
    material: FrozenTaskMaterial,
) -> dict[str, Any]:
    started = time.monotonic()
    attempt_dir = output_dir / "attempts" / task.id / str(repetition)
    run_dir = output_dir / "runs" / task.id / str(repetition)
    attempt_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    acceptance_files = dict(material.acceptance_files)
    workspace: BenchmarkWorkspace | None = None
    response = ""
    events: list[dict[str, Any]] = []
    actual_usage: dict[str, int] | None = None
    context_telemetry: dict[str, Any] = _empty_context_telemetry()
    context_reductions = 0
    tool_errors = 0
    turns = 0
    patch = ""
    artifact_patch = ""
    patch_redacted = False
    verifier_output = ""
    execution_status = "completed"
    verification_status = "not_run"
    error: str | None = None
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
                snapshot_dir=run_dir / "snapshots",
                secrets=secrets,
            )
        )
        response = agent_result["response"]
        events = agent_result["events"]
        actual_usage = agent_result["actual_usage"]
        context_telemetry = agent_result["context_telemetry"]
        context_reductions = int(agent_result["context_reductions"])
        tool_errors = int(agent_result["tool_errors"])
        turns = int(agent_result["turns"])
        if agent_result["error"]:
            execution_status = "agent_error"
            error = str(agent_result["error"])
        patch = collect_benchmark_patch(workspace)
        artifact_patch = patch
        if _contains_sensitive_text(patch, secrets):
            patch_redacted = True
            artifact_patch = ""
            execution_status = "agent_error"
            verification_status = "not_run"
            error = "Agent patch contained a configured credential; patch artifact omitted"
        elif execution_status == "completed":
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
        "task_id": task.id,
        "repetition": repetition,
        "execution_status": execution_status,
        "verification_status": verification_status,
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "turns": turns,
        "actual_usage": actual_usage,
        "context_telemetry": context_telemetry,
        "context_reductions": context_reductions,
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


async def _execute_production_agent(
    prompt: str,
    workspace: Path,
    config: PaiCliConfig,
    client: Any,
    *,
    snapshot_dir: Path,
    secrets: set[str],
) -> dict[str, Any]:
    registry = _benchmark_tool_registry()
    engine = QueryEngine(
        llm_client=client,
        tool_registry=registry,
        config=config,
        cwd=str(workspace),
    )
    response = ""
    events: list[dict[str, Any]] = []
    input_tokens = 0
    output_tokens = 0
    usage_seen = False
    context_telemetry = _empty_context_telemetry()
    context_reductions = 0
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
            async for event in engine.ask(prompt):
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
        "tool_errors": tool_errors,
        "turns": turns,
        "error": error,
    }


def _benchmark_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in get_builtin_tools():
        if tool.name in TOOL_PROFILE:
            registry.register(tool)
    return registry


def _configure_benchmark(config: PaiCliConfig, output_dir: Path) -> None:
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
        "| Task | Repetition | Execution | Verification | Tokens | Seconds |",
        "| --- | ---: | --- | --- | ---: | ---: |",
    ]
    for attempt in payload["attempts"]:
        usage = attempt.get("actual_usage") or {}
        token_text = usage.get("total_tokens", "n/a")
        lines.append(
            f"| `{attempt['task_id']}` | {attempt['repetition']} | "
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
    "load_local_smoke_suite",
    "local_smoke_exit_code",
    "materialize_benchmark_workspace",
    "run_local_smoke",
]
