"""Scripted end-to-end evaluation for PaiCLI context-cost strategies."""

from __future__ import annotations

import asyncio
import json
import shutil
import statistics
import subprocess
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from paicli.agent.query import query
from paicli.config import PaiCliConfig
from paicli.context import ContextBuildResult, ContextManager
from paicli.context.compaction import CompactionResult, deterministic_compact, extract_delta_items
from paicli.context.token_estimator import TokenEstimator
from paicli.tools import ToolRegistry, get_builtin_tools
from paicli.types import Message

VARIANTS = (
    "no_context_reduction",
    "full_orchestrator",
    "full_orchestrator_with_llm_handoff",
)
BENCHMARK_SYSTEM_PROMPT = "You are executing a scripted context-cost benchmark."
SYNTHETIC_PRESSURE_TRIGGER_RATIO = 0.95
MIN_COMPACTION_HISTORY_ITEMS = 6
SYNTHETIC_TURN_CONTENT_CHARS = 6_000


@dataclass(frozen=True)
class ScriptedAction:
    type: str
    name: str = ""
    arguments: dict[str, Any] | None = None
    text: str = ""


class _BaselineContextManager:
    """Benchmark-only full-history strategy."""

    def __init__(self) -> None:
        self.trace_decisions: list[dict[str, Any]] = []

    async def build_turn_context(
        self,
        *,
        prefix: str = "",
        messages: list[Message] | None = None,
        **_ignored: Any,
    ) -> ContextBuildResult:
        result = ContextBuildResult(
            system_prompt=prefix,
            messages=list(messages or []),
            compacted=False,
            pressure_tier="disabled",
        )
        self.trace_decisions.append(
            {
                "event": "context_decision",
                "pressure_tier": result.pressure_tier,
                "compacted": False,
                "summary_mode": "none",
                "summary_usage": [],
            }
        )
        return result

    def get_status(self) -> dict[str, Any]:
        return {"last_compaction": {"used_llm": False, "compacted_items": 0}}


class _BenchmarkContextManager(ContextManager):
    """Use raw role-preserving history for benchmark-only pressure triggering."""

    async def build_turn_context(
        self,
        *,
        prefix: str = "",
        messages: list[Message] | None = None,
        **kwargs: Any,
    ) -> ContextBuildResult:
        all_messages = list(messages or [])
        result = await super().build_turn_context(prefix=prefix, messages=all_messages, **kwargs)
        if result.compacted:
            return self._record_decision(result)
        history, current = _split_current_request(all_messages)
        raw_text = prefix + "\n" + "\n".join(str(item.content) for item in all_messages)
        raw_tokens = self._token_estimator.estimate(raw_text)
        budget = self._calculate_budget()
        if (
            raw_tokens < budget.prompt_tokens * SYNTHETIC_PRESSURE_TRIGGER_RATIO
            or len(history) < MIN_COMPACTION_HISTORY_ITEMS
        ):
            return self._record_decision(result)
        compacted = await self._compact_messages(self._compress_tool_results(history))
        if compacted is None:
            return self._record_decision(result)
        return self._record_decision(
            ContextBuildResult(
                system_prompt=prefix,
                messages=[*compacted, *([current] if current else [])],
                compacted=True,
                pressure_tier="tier3_summary",
            )
        )

    def _record_decision(self, result: ContextBuildResult) -> ContextBuildResult:
        if not hasattr(self, "trace_decisions"):
            self.trace_decisions: list[dict[str, Any]] = []
        compaction = self._last_compaction if result.compacted else None
        self.trace_decisions.append(
            {
                "event": "context_decision",
                "pressure_tier": result.pressure_tier,
                "compacted": result.compacted,
                "summary_mode": (
                    "llm"
                    if compaction and compaction.used_llm
                    else "deterministic"
                    if compaction
                    else "none"
                ),
                "summary_usage": compaction.llm_usage if compaction else {},
            }
        )
        return result


class _DeterministicContextManager(_BenchmarkContextManager):
    """Reuse production pressure handling but force the no-model compactor."""

    async def _compact_messages(self, history: list[Message]) -> list[Message] | None:
        delta_items, protected_items = extract_delta_items(
            history,
            protected_turns=self.config.context.protected_turns,
        )
        if not delta_items:
            return None
        result = deterministic_compact(delta_items, prior_summary=self._current_summary)
        result.protected_items = len(protected_items)
        self._last_compaction = result
        self._current_summary = result.summary
        return [
            Message(role="system", content=f"[Previous conversation summary]\n{result.summary}"),
            *[
                Message(role=item.role, content=item.content, tool_call_id=item.tool_call_id)
                for item in protected_items
            ],
        ]


class _RecordedHandoffContextManager(_BenchmarkContextManager):
    """Replay exactly one reviewed handoff summary instead of map-reducing it."""

    async def _compact_messages(self, history: list[Message]) -> list[Message] | None:
        delta_items, protected_items = extract_delta_items(
            history,
            protected_turns=self.config.context.protected_turns,
        )
        if not delta_items:
            return None
        summary, usage = self.llm_client.next_handoff()
        result = CompactionResult(
            summary=summary,
            compacted_items=len(delta_items),
            protected_items=len(protected_items),
            used_llm=True,
            llm_usage=usage,
        )
        self._last_compaction = result
        self._current_summary = result.summary
        return [
            Message(role="system", content=f"[Previous conversation summary]\n{result.summary}"),
            *[
                Message(role=item.role, content=item.content, tool_call_id=item.tool_call_id)
                for item in protected_items
            ],
        ]


class _ScriptedClient:
    """Fixed model responses plus declared handoff-summary token usage."""

    model_name = "scripted-context-cost"
    provider_name = "scripted"
    max_context_window = 20_000

    def __init__(self, actions: list[ScriptedAction], handoff: dict[str, Any] | None):
        self._actions = list(actions)
        self._handoff = dict(handoff or {})
        self.requests: list[dict[str, Any]] = []
        self.compaction_usages: list[dict[str, int]] = []

    def next_handoff(self) -> tuple[str, dict[str, int]]:
        usage = _usage_dict(self._handoff.get("usage"))
        self.compaction_usages.append(usage)
        return str(self._handoff.get("summary") or ""), usage

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        system_prompt: str,
    ) -> AsyncIterator[dict[str, Any]]:
        self.requests.append(
            {
                "system_prompt": system_prompt,
                "messages": list(messages),
                "tools": list(tools),
            }
        )
        if not self._actions:
            yield {"type": "error", "error": RuntimeError("scripted model ran out of outputs")}
            return
        action = self._actions.pop(0)
        if action.type == "tool":
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": f"scripted_call_{len(self.requests)}",
                    "function": {
                        "name": action.name,
                        "arguments": json.dumps(action.arguments or {}, ensure_ascii=False),
                    },
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return
        if action.type == "final":
            yield {"type": "text_delta", "text": action.text}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        yield {
            "type": "error",
            "error": RuntimeError(f"unsupported scripted action: {action.type}"),
        }


def run_scripted_context_cost(
    manifest_path: str | Path,
    *,
    output_dir: str | Path,
    repetitions: int = 2,
) -> dict[str, Any]:
    """Run all scripted tasks and write reproducible proxy-cost artifacts."""
    if repetitions < 2:
        raise ValueError("scripted context-cost evaluation requires at least two repetitions")
    manifest_file = Path(manifest_path).resolve()
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    tasks = list(manifest.get("tasks") or [])
    if not tasks:
        raise ValueError("context-cost manifest must contain at least one task")
    target = Path(output_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for repeat in range(int(repetitions)):
        for task in tasks:
            for variant in VARIANTS:
                rows.append(
                    _run_task(
                        task,
                        variant=variant,
                        repeat=repeat,
                        manifest_root=manifest_file.parent,
                        output_dir=target,
                    )
                )
    determinism = _check_determinism(rows)
    payload = {
        "artifact_type": "scripted-context-cost-evaluation",
        "usage_source": "estimated_proxy",
        "rows": rows,
        "determinism": determinism,
        "summary": _summarize(rows),
    }
    (target / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (target / "report.md").write_text(_render_report(payload), encoding="utf-8")
    return payload


def _run_task(
    task: dict[str, Any],
    *,
    variant: str,
    repeat: int,
    manifest_root: Path,
    output_dir: Path,
) -> dict[str, Any]:
    task_id = str(task["id"])
    source = _fixture_path(manifest_root, str(task["fixture_repo"]))
    workspace = output_dir / "runs" / task_id / variant / str(repeat) / "workspace"
    _copy_fixture(source, workspace)
    actions = [_parse_action(item) for item in list(task.get("scripted_outputs") or [])]
    client = _ScriptedClient(actions, task.get("llm_handoff"))
    config = _benchmark_config(workspace)
    registry = _tool_registry(task.get("allowed_tools") or [])
    manager = _context_manager(variant, config=config, client=client, cwd=workspace)
    history = _synthetic_pressure_history(config, client)
    events = asyncio.run(
        _run_query(
            client=client,
            registry=registry,
            config=config,
            workspace=workspace,
            history=history,
            prompt=str(task["prompt"]),
            manager=manager,
            max_turns=int(task.get("step_budget") or 20),
        )
    )
    tool_failed = any(
        event.get("type") == "tool_result" and bool(event.get("is_error")) for event in events
    )
    final_text = "".join(
        str(event.get("text") or "") for event in events if event.get("type") == "text_delta"
    )
    finished = any(event.get("type") == "done" for event in events) and bool(final_text)
    verifier = _run_verifier(task.get("verifier"), workspace)
    status = "passed" if finished and not tool_failed and verifier.returncode == 0 else "failed"
    estimator = TokenEstimator()
    requests = [_request_trace(request, estimator) for request in client.requests]
    summary_tokens = sum(
        int(usage["input_tokens"]) + int(usage["output_tokens"])
        for usage in client.compaction_usages
    )
    trace = [
        {
            "event": "model_request",
            "request_index": index + 1,
            **request,
        }
        for index, request in enumerate(requests)
    ]
    trace.extend(
        {"turn": index + 1, **decision} for index, decision in enumerate(manager.trace_decisions)
    )
    trace.append(
        {
            "event": "compaction",
            "variant": variant,
            "summary_called": bool(client.compaction_usages),
            "summary_usage": client.compaction_usages,
        }
    )
    trace.append(
        {
            "event": "verification",
            "status": status,
            "verifier_returncode": verifier.returncode,
            "tool_failed": tool_failed,
            "finished": finished,
        }
    )
    trace_path = output_dir / "traces" / task_id / variant / f"{repeat}.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(
        "".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in trace),
        encoding="utf-8",
    )
    return {
        "task_id": task_id,
        "variant": variant,
        "repeat": repeat,
        "status": status,
        "usage_source": "estimated_proxy",
        "input_tokens": sum(item["input_tokens"] for item in requests),
        "model_call_count": len(requests),
        "compact_call_total_tokens": summary_tokens,
        "summary_called": bool(client.compaction_usages),
        "trace_path": str(trace_path),
        "workspace": str(workspace),
        "verifier_returncode": verifier.returncode,
    }


async def _run_query(
    *,
    client: _ScriptedClient,
    registry: ToolRegistry,
    config: PaiCliConfig,
    workspace: Path,
    history: list[Message],
    prompt: str,
    manager: ContextManager | _BaselineContextManager,
    max_turns: int,
) -> list[dict[str, Any]]:
    return [
        event
        async for event in query(
            llm_client=client,
            tool_registry=registry,
            system_prompt=BENCHMARK_SYSTEM_PROMPT,
            user_message=prompt,
            history=history,
            cwd=str(workspace),
            config=config,
            max_turns=max_turns,
            context_manager=manager,
        )
    ]


def _benchmark_config(workspace: Path) -> PaiCliConfig:
    config = PaiCliConfig()
    config.features.mcp = False
    config.features.memory = False
    config.features.skill = False
    config.policy.hitl_mode = "never"
    config.policy.audit_log_path = str(workspace / ".audit")
    config.context.tool_result_storage_dir = str(workspace / ".tool_results")
    config.context.protected_turns = 2
    return config


def _tool_registry(allowed_tools: list[str]) -> ToolRegistry:
    allowed = {str(name) for name in allowed_tools}
    registry = ToolRegistry()
    registry.register_all([tool for tool in get_builtin_tools() if tool.name in allowed])
    return registry


def _context_manager(
    variant: str,
    *,
    config: PaiCliConfig,
    client: _ScriptedClient,
    cwd: Path,
) -> ContextManager | _BaselineContextManager:
    if variant == "no_context_reduction":
        return _BaselineContextManager()
    if variant == "full_orchestrator":
        return _DeterministicContextManager(config=config, llm_client=client, cwd=str(cwd))
    if variant == "full_orchestrator_with_llm_handoff":
        return _RecordedHandoffContextManager(config=config, llm_client=client, cwd=str(cwd))
    raise ValueError(f"unknown context-cost variant: {variant}")


def _synthetic_pressure_history(config: PaiCliConfig, client: _ScriptedClient) -> list[Message]:
    manager = ContextManager(config=config, llm_client=client, cwd=".")
    target_tokens = manager._calculate_budget().prompt_tokens
    estimator = TokenEstimator()
    history: list[Message] = []
    index = 0
    while (
        estimator.estimate("\n".join(str(message.content) for message in history)) < target_tokens
    ):
        prompt_with_history = (
            BENCHMARK_SYSTEM_PROMPT + "\n" + "\n".join(str(message.content) for message in history)
        )
        if (
            len(history) >= MIN_COMPACTION_HISTORY_ITEMS
            and estimator.estimate(prompt_with_history)
            >= target_tokens * SYNTHETIC_PRESSURE_TRIGGER_RATIO
        ):
            return history
        history.append(
            Message(
                role="user",
                content=(
                    f"synthetic pressure request {index}: " + ("u" * SYNTHETIC_TURN_CONTENT_CHARS)
                ),
            )
        )
        history.append(
            Message(
                role="assistant",
                content=(
                    f"synthetic pressure response {index}: " + ("a" * SYNTHETIC_TURN_CONTENT_CHARS)
                ),
            )
        )
        index += 1
    return history


def _parse_action(value: dict[str, Any]) -> ScriptedAction:
    action_type = str(value.get("type") or "")
    if action_type == "tool":
        return ScriptedAction(
            type="tool",
            name=str(value.get("name") or ""),
            arguments=dict(value.get("arguments") or {}),
        )
    if action_type == "final":
        return ScriptedAction(type="final", text=str(value.get("text") or ""))
    raise ValueError(f"unsupported scripted action: {action_type}")


def _usage_dict(value: Any) -> dict[str, int]:
    raw = value if isinstance(value, dict) else {}
    return {
        "input_tokens": int(raw.get("input_tokens") or 0),
        "output_tokens": int(raw.get("output_tokens") or 0),
    }


def _fixture_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_dir():
        raise ValueError(f"fixture_repo must be a directory inside the manifest root: {relative}")
    return path


def _copy_fixture(source: Path, workspace: Path) -> None:
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, workspace)


def _run_verifier(value: Any, workspace: Path) -> subprocess.CompletedProcess[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("scripted context-cost verifier must be a command array")
    return subprocess.run(
        value, cwd=workspace, capture_output=True, text=True, timeout=30, check=False
    )


def _request_trace(request: dict[str, Any], estimator: TokenEstimator) -> dict[str, int]:
    rendered = {
        "system": request["system_prompt"],
        "messages": [asdict(message) for message in request["messages"]],
        "tools": request["tools"],
    }
    return {
        "input_tokens": estimator.estimate(json.dumps(rendered, ensure_ascii=False, sort_keys=True))
    }


def _check_determinism(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_key[(str(row["task_id"]), str(row["variant"]))].append(row)
    failures = []
    for key, group in by_key.items():
        if len(group) < 2:
            failures.append({"task_id": key[0], "variant": key[1], "reason": "missing repeat"})
            continue
        first = _normalized_trace(Path(group[0]["trace_path"]), Path(group[0]["workspace"]))
        first_metrics = _core_metrics(group[0])
        for row in group[1:]:
            other = _normalized_trace(Path(row["trace_path"]), Path(row["workspace"]))
            if other != first or _core_metrics(row) != first_metrics:
                failures.append({"task_id": key[0], "variant": key[1], "reason": "repeat mismatch"})
                break
    return {"passed": not failures, "failures": failures}


def _normalized_trace(path: Path, workspace: Path) -> str:
    return path.read_text(encoding="utf-8").replace(str(workspace), "<workspace>")


def _core_metrics(row: dict[str, Any]) -> dict[str, Any]:
    return {
        field: row[field]
        for field in (
            "status",
            "input_tokens",
            "model_call_count",
            "compact_call_total_tokens",
            "summary_called",
            "verifier_returncode",
        )
    }


def _split_current_request(messages: list[Message]) -> tuple[list[Message], Message | None]:
    if messages and messages[-1].role == "user":
        return messages[:-1], messages[-1]
    return messages, None


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_variant: dict[str, dict[str, int]] = defaultdict(
        lambda: {"runs": 0, "passed": 0, "input_tokens": 0}
    )
    for row in rows:
        bucket = by_variant[str(row["variant"])]
        bucket["runs"] += 1
        bucket["passed"] += int(row["status"] == "passed")
        bucket["input_tokens"] += int(row["input_tokens"])
    for variant, bucket in by_variant.items():
        compact = sum(
            int(row["compact_call_total_tokens"]) for row in rows if row["variant"] == variant
        )
        bucket["compact_call_total_tokens"] = compact
    return {
        "by_variant": dict(by_variant),
        "comparisons": {
            "full_history_vs_deterministic": _paired_comparison(
                rows,
                treatment="full_orchestrator",
                control="no_context_reduction",
            ),
            "deterministic_vs_llm_handoff": _paired_comparison(
                rows,
                treatment="full_orchestrator_with_llm_handoff",
                control="full_orchestrator",
            ),
        },
    }


def _paired_comparison(
    rows: list[dict[str, Any]],
    *,
    treatment: str,
    control: str,
) -> dict[str, Any]:
    pairs: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        pairs[(str(row["task_id"]), int(row["repeat"]))][str(row["variant"])] = row
    net_values: list[int] = []
    passed_pairs = 0
    for pair in pairs.values():
        if treatment not in pair or control not in pair:
            continue
        treated = pair[treatment]
        baseline = pair[control]
        net_values.append(
            int(baseline["input_tokens"])
            - int(treated["input_tokens"])
            - int(treated["compact_call_total_tokens"])
        )
        passed_pairs += int(treated["status"] == "passed" and baseline["status"] == "passed")
    return {
        "treatment": treatment,
        "control": control,
        "paired_run_count": len(net_values),
        "passed_pair_count": passed_pairs,
        "median_net_benefit_tokens": statistics.median(net_values) if net_values else 0,
        "net_benefit_tokens": net_values,
    }


def _render_report(payload: dict[str, Any]) -> str:
    summary = payload["summary"]["by_variant"]
    comparisons = payload["summary"]["comparisons"]
    lines = [
        "# 脚本上下文成本评测",
        "",
        "所有 token 均为 `estimated_proxy`，不是 provider 账单数据。",
        "",
        "| 变体 | 通过/总数 | 输入 token | 摘要 token | 相对全历史净收益 |",
        "|---|---:|---:|---:|---:|",
    ]
    for variant in VARIANTS:
        item = summary.get(variant, {})
        lines.append(
            f"| `{variant}` | {item.get('passed', 0)}/{item.get('runs', 0)} | "
            f"{item.get('input_tokens', 0)} | {item.get('compact_call_total_tokens', 0)} | "
            "见配对比较 |"
        )
    lines.extend(
        [
            "",
            "## 配对比较",
            "",
        ]
    )
    for name, comparison in comparisons.items():
        lines.extend(
            [
                f"- `{name}`：{comparison['treatment']} 相对 {comparison['control']}，"
                f"有效配对 {comparison['paired_run_count']}，"
                f"中位净收益 {comparison['median_net_benefit_tokens']} tokens。",
            ]
        )
    lines.extend(
        [
            "",
            f"确定性校验：{'通过' if payload['determinism']['passed'] else '失败'}。",
            "脚本 verifier 只覆盖固定轨迹；真实成本优化仍需 live 模式的 provider `actual` usage。",
            "",
        ]
    )
    return "\n".join(lines)
