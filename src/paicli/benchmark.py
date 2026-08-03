from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from paicli import __version__
from paicli.agent import QueryEngine
from paicli.bootstrap import build_tool_registry
from paicli.clock import now_timestamp
from paicli.config import PaiCliConfig
from paicli.llm import create_llm_client

DEFAULT_MAX_TURNS = 100
DEFAULT_MAX_TOOL_CALLS = 200
DEFAULT_MAX_ELAPSED_SECONDS = 1800.0
DEFAULT_MAX_TOTAL_TOKENS = 1_000_000


@dataclass(frozen=True, slots=True)
class BenchmarkOptions:
    log_dir: Path
    max_turns: int = DEFAULT_MAX_TURNS
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    max_elapsed_seconds: float = DEFAULT_MAX_ELAPSED_SECONDS
    max_total_tokens: int = DEFAULT_MAX_TOTAL_TOKENS
    source_identity_file: Path | None = None
    mcp_config_file: Path | None = None


async def run_benchmark_prompt(
    prompt: str,
    *,
    cwd: str | Path,
    config: PaiCliConfig,
    options: BenchmarkOptions,
) -> str:
    """Run one unattended production Agent turn and persist Harbor diagnostics."""

    if not config.llm.api_key:
        raise ValueError("PAICLI_API_KEY is required for benchmark mode")
    _configure_benchmark_runtime(config, options)
    log_dir = options.log_dir.resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    secrets = _secret_values()
    events_path = log_dir / "events.jsonl"
    readable_path = log_dir / "paicli.txt"
    events_path.write_text("", encoding="utf-8")
    readable_path.write_text("", encoding="utf-8")

    registry, manager = await build_tool_registry(
        config=config,
        cwd=str(Path(cwd).resolve()),
        mcp_config_paths=[options.mcp_config_file] if options.mcp_config_file else [],
    )
    client = create_llm_client(
        config.llm,
        retry_policy=config.retry.resolve("llm"),
        retry_audit_path=config.policy.audit_log_path,
        retry_cwd=str(cwd),
    )
    runtime = _runtime_metadata(config, options, registry.list_names(), manager)
    runtime["status"] = "running"
    _write_json(log_dir / "runtime.json", runtime)
    engine = QueryEngine(
        llm_client=client,
        tool_registry=registry,
        config=config,
        cwd=str(Path(cwd).resolve()),
        approval_callback=_approve_unattended,
    )

    text = ""
    try:
        async for event in engine.ask(prompt):
            safe_event = _sanitize(_jsonable(event), secrets)
            _append_jsonl(events_path, safe_event)
            _append_readable(readable_path, safe_event)
            event_type = event.get("type")
            if event_type == "text_delta":
                text += str(event.get("text") or "")
            elif event_type == "error":
                error = event.get("error")
                raise error if isinstance(error, Exception) else RuntimeError(str(error))
        safe_text = _sanitize(text, secrets)
        (log_dir / "final.txt").write_text(safe_text + "\n", encoding="utf-8")
        runtime["status"] = "completed"
        runtime["completed_at"] = now_timestamp()
        _write_json(log_dir / "runtime.json", runtime)
        _append_readable(readable_path, {"type": "completed"})
        return safe_text
    except BaseException as exc:
        runtime["status"] = "failed"
        runtime["completed_at"] = now_timestamp()
        runtime["error"] = _sanitize(str(exc), secrets)
        _write_json(log_dir / "runtime.json", runtime)
        _append_jsonl(
            events_path,
            {"type": "benchmark_error", "error": _sanitize(str(exc), secrets)},
        )
        _append_readable(
            readable_path,
            {"type": "failed", "error": _sanitize(str(exc), secrets)},
        )
        raise


def _configure_benchmark_runtime(config: PaiCliConfig, options: BenchmarkOptions) -> None:
    config.render_mode = "plain"
    config.agent.max_turns = options.max_turns
    config.agent.max_tool_calls = options.max_tool_calls
    config.agent.max_elapsed_seconds = options.max_elapsed_seconds
    config.agent.max_total_tokens = options.max_total_tokens
    config.policy.hitl_mode = "never"
    config.policy.require_approval_for_writes = False
    config.policy.audit_log_path = str(options.log_dir / "audit")
    state_dir = options.log_dir / "state"
    config.memory.long_term_path = str(state_dir / "memory" / "long_term_memory.json")
    config.memory.long_term_db_path = str(state_dir / "memory.db")
    config.context.tool_result_storage_dir = str(state_dir / "tool_results")
    os.environ["PAICLI_SNAPSHOT_DIR"] = str(state_dir / "snapshots")


def _approve_unattended(_request: dict[str, Any]) -> str:
    return "approve"


def _runtime_metadata(config, options, tools, manager) -> dict[str, Any]:
    dependencies = sorted(
        f"{distribution.metadata.get('Name', '')}=={distribution.version}"
        for distribution in importlib.metadata.distributions()
        if distribution.metadata.get("Name")
    )
    identity = _read_source_identity(options.source_identity_file)
    configuration = {
        "provider": config.llm.provider,
        "model": config.llm.model,
        "base_url_hash": _hash_text(config.llm.base_url or ""),
        "temperature": config.llm.temperature,
        "max_tokens": config.llm.max_tokens,
        "thinking_level": config.llm.thinking_level,
        "thinking_budget": config.llm.thinking_budget,
        "context_window": config.llm.context_window,
        "tools": sorted(tools),
        "mcp_servers": sorted(manager.specs) if manager else [],
        "agent_budget": {
            "max_turns": config.agent.max_turns,
            "max_tool_calls": config.agent.max_tool_calls,
            "max_elapsed_seconds": config.agent.max_elapsed_seconds,
            "max_total_tokens": config.agent.max_total_tokens,
        },
    }
    return {
        "schema_version": 1,
        "started_at": now_timestamp(),
        "paicli_version": __version__,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "provider": config.llm.provider,
        "model": config.llm.model,
        "temperature": config.llm.temperature,
        "tools": sorted(tools),
        "mcp_servers": configuration["mcp_servers"],
        "agent_budget": configuration["agent_budget"],
        "configuration_fingerprint": _hash_json(configuration),
        "dependency_fingerprint": _hash_json(dependencies),
        "source_identity": identity,
    }


def _read_source_identity(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _secret_values() -> tuple[str, ...]:
    markers = ("API_KEY", "AUTHORIZATION", "PASSWORD", "PRIVATE_KEY", "SECRET")
    values = {
        value
        for key, value in os.environ.items()
        if value
        and (
            any(marker in key.upper() for marker in markers)
            or key.upper() == "TOKEN"
            or key.upper().endswith("_TOKEN")
        )
    }
    return tuple(sorted(values, key=len, reverse=True))


def _sanitize(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {str(key): _sanitize(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item, secrets) for item in value]
    return value


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(item) for item in value]
    if isinstance(value, BaseException):
        return f"{type(value).__name__}: {value}"
    return str(value)


def _append_jsonl(path: Path, value: Any) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def _append_readable(path: Path, event: dict[str, Any]) -> None:
    event_type = str(event.get("type") or "event")
    detail = event.get("text") or event.get("error") or event.get("content") or ""
    line = f"[{event_type}]"
    if detail:
        line += f" {detail}"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _hash_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


__all__ = ["BenchmarkOptions", "run_benchmark_prompt"]
