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
from paicli.context.compaction import CompactionResult, DeltaItem, deterministic_compact
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
    """Record decisions made by the production pressure-governance pipeline."""

    async def build_turn_context(
        self,
        *,
        prefix: str = "",
        messages: list[Message] | None = None,
        **kwargs: Any,
    ) -> ContextBuildResult:
        result = await super().build_turn_context(
            prefix=prefix,
            messages=list(messages or []),
            **kwargs,
        )
        return self._record_decision(result)

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

    async def _create_compaction(
        self,
        delta_items: list[DeltaItem],
        prior_summary: str,
    ) -> CompactionResult:
        return deterministic_compact(delta_items, prior_summary=prior_summary)


class _RecordedHandoffContextManager(_BenchmarkContextManager):
    """Replay exactly one reviewed handoff summary instead of map-reducing it."""

    async def _create_compaction(
        self,
        delta_items: list[DeltaItem],
        prior_summary: str,
    ) -> CompactionResult:
        del prior_summary
        summary, usage = self.llm_client.next_handoff()
        return CompactionResult(
            summary=summary,
            compacted_items=len(delta_items),
            protected_items=0,
            used_llm=True,
            llm_usage=usage,
        )


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


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_variant: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "runs": 0,
            "passed": 0,
            "input_tokens": 0,
            "model_call_count": 0,
            "summary_called_runs": 0,
            "compact_call_total_tokens": 0,
        }
    )
    for row in rows:
        bucket = by_variant[str(row["variant"])]
        bucket["runs"] += 1
        bucket["passed"] += int(row["status"] == "passed")
        bucket["input_tokens"] += int(row["input_tokens"])
        bucket["model_call_count"] += int(row["model_call_count"])
        bucket["summary_called_runs"] += int(bool(row["summary_called"]))
        bucket["compact_call_total_tokens"] += int(row["compact_call_total_tokens"])
    return {
        "by_variant": dict(by_variant),
        "quality": {
            "expected_runs": len(rows),
            "passed_runs": sum(row["status"] == "passed" for row in rows),
            "verifier_passed_runs": sum(
                int(row["verifier_returncode"]) == 0 for row in rows
            ),
            "task_count": len({str(row["task_id"]) for row in rows}),
            "repetitions": len({int(row["repeat"]) for row in rows}),
        },
        "comparisons": {
            "full_history_vs_deterministic": _paired_comparison(
                rows,
                treatment="full_orchestrator",
                control="no_context_reduction",
            ),
            "full_history_vs_llm_handoff": _paired_comparison(
                rows,
                treatment="full_orchestrator_with_llm_handoff",
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
    input_reduction_values: list[int] = []
    summary_cost_values: list[int] = []
    passed_pairs = 0
    pair_details: list[dict[str, Any]] = []
    for (task_id, repeat), pair in sorted(pairs.items()):
        if treatment not in pair or control not in pair:
            continue
        treated = pair[treatment]
        baseline = pair[control]
        input_reduction = int(baseline["input_tokens"]) - int(treated["input_tokens"])
        summary_cost = int(treated["compact_call_total_tokens"])
        net_benefit = input_reduction - summary_cost
        pair_passed = treated["status"] == "passed" and baseline["status"] == "passed"
        input_reduction_values.append(input_reduction)
        summary_cost_values.append(summary_cost)
        net_values.append(net_benefit)
        passed_pairs += int(pair_passed)
        pair_details.append(
            {
                "task_id": task_id,
                "repeat": repeat,
                "control_input_tokens": int(baseline["input_tokens"]),
                "treatment_input_tokens": int(treated["input_tokens"]),
                "summary_call_tokens": summary_cost,
                "input_reduction_tokens": input_reduction,
                "net_benefit_tokens": net_benefit,
                "passed": pair_passed,
            }
        )
    return {
        "treatment": treatment,
        "control": control,
        "paired_run_count": len(net_values),
        "passed_pair_count": passed_pairs,
        "median_input_reduction_tokens": statistics.median(input_reduction_values)
        if input_reduction_values
        else 0,
        "median_summary_call_tokens": statistics.median(summary_cost_values)
        if summary_cost_values
        else 0,
        "median_net_benefit_tokens": statistics.median(net_values) if net_values else 0,
        "net_benefit_tokens": net_values,
        "pairs": pair_details,
    }


def _render_report(payload: dict[str, Any]) -> str:
    summary = payload["summary"]["by_variant"]
    quality = payload["summary"]["quality"]
    comparisons = payload["summary"]["comparisons"]
    baseline_comparisons = {
        "no_context_reduction": None,
        "full_orchestrator": comparisons["full_history_vs_deterministic"],
        "full_orchestrator_with_llm_handoff": comparisons["full_history_vs_llm_handoff"],
    }
    lines = [
        "# 脚本化上下文成本评测报告",
        "",
        "## 结论与边界",
        "",
        (
            f"本次运行覆盖 **{quality['task_count']}** 个隔离夹具任务、"
            f"**{len(VARIANTS)}** 个策略、每个策略 **{quality['repetitions']}** 次重复，"
            f"共 **{quality['expected_runs']}** 次脚本运行。"
        ),
        (
            f"质量门槛通过 **{quality['passed_runs']}/{quality['expected_runs']}** 次；"
            f"verifier 通过 **{quality['verifier_passed_runs']}/{quality['expected_runs']}** 次；"
            f"确定性校验：{'通过' if payload['determinism']['passed'] else '失败'}。"
        ),
        "",
        "所有 token 均为 `estimated_proxy`，并非 provider 返回的 `actual` usage 或账单数据。",
        "因此本报告只能说明固定脚本轨迹下的可复现方向性证据，不能据此宣称真实 provider 成本优化。",
        "",
        "## 实验口径",
        "",
        "- 每次运行复制任务 fixture，在副本中执行真实原生工具，最后执行该任务的 verifier。",
        "- 质量通过条件：脚本工具调用无错误、Agent 正常输出最终文本、verifier 返回码为 0。",
        (
            "- `输入 token`：每轮实际发送给脚本模型的 role-preserving 请求，"
            "以固定 `TokenEstimator` 估算后累加。"
        ),
        (
            "- `摘要生成调用成本`：仅指为生成摘要额外调用 LLM 的 input + output token。"
            "确定性摘要的该值为 `0`，因为它只执行本地规则；"
            "摘要文本随后随请求发送时，仍包含在 `输入 token` 中。"
        ),
        (
            "- `净收益`：`对照组输入 token - 处理组输入 token - "
            "处理组摘要生成调用成本`；允许为负，不会截断为 0。"
        ),
        "",
        "## 策略汇总",
        "",
        (
            "| 变体 | 通过/运行 | 输入 token（总计） | 每次平均输入 token | 模型调用次数 | "
            "摘要调用运行数 | 摘要生成调用成本 token | 相对全历史中位净收益 |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in VARIANTS:
        item = summary.get(variant, {})
        runs = int(item.get("runs", 0))
        comparison = baseline_comparisons[variant]
        net_benefit = "基线" if comparison is None else _format_number(
            comparison["median_net_benefit_tokens"]
        )
        lines.append(
            f"| `{variant}` | {item.get('passed', 0)}/{runs} | "
            f"{_format_number(item.get('input_tokens', 0))} | "
            f"{_format_number(int(item.get('input_tokens', 0)) / runs if runs else 0)} | "
            f"{_format_number(item.get('model_call_count', 0))} | "
            f"{item.get('summary_called_runs', 0)} | "
            f"{_format_number(item.get('compact_call_total_tokens', 0))} | {net_benefit} |"
        )
    lines.extend(
        [
            "",
            "## 配对比较与净收益",
            "",
            (
                "| 比较（对照 → 处理） | 有效配对 | 同时通过 | 中位输入减少 token | "
                "中位摘要生成调用成本 token | 中位净收益 token |"
            ),
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for name, comparison in comparisons.items():
        lines.append(
            f"| `{name}` (`{comparison['control']}` → `{comparison['treatment']}`) | "
            f"{comparison['paired_run_count']} | {comparison['passed_pair_count']} | "
            f"{_format_number(comparison['median_input_reduction_tokens'])} | "
            f"{_format_number(comparison['median_summary_call_tokens'])} | "
            f"{_format_number(comparison['median_net_benefit_tokens'])} |"
        )
    lines.extend(
        [
            "",
            "## 逐任务配对结果",
            "",
            "下表为每个任务跨重复次数的中位值；净收益均相对 `no_context_reduction` 计算。",
            "",
            (
                "| 任务 | 质量通过 | 全历史输入 token | 确定性摘要输入 token | "
                "LLM handoff 输入 token | 确定性净收益 | "
                "LLM handoff 摘要调用成本 | LLM handoff 净收益 |"
            ),
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for task_id in sorted({str(row["task_id"]) for row in payload["rows"]}):
        task_rows = [row for row in payload["rows"] if row["task_id"] == task_id]
        values = {
            variant: [row for row in task_rows if row["variant"] == variant]
            for variant in VARIANTS
        }
        baseline_input = _median_field(values["no_context_reduction"], "input_tokens")
        deterministic_input = _median_field(values["full_orchestrator"], "input_tokens")
        handoff_input = _median_field(values["full_orchestrator_with_llm_handoff"], "input_tokens")
        handoff_summary_cost = _median_field(
            values["full_orchestrator_with_llm_handoff"], "compact_call_total_tokens"
        )
        deterministic_net = baseline_input - deterministic_input
        handoff_net = baseline_input - handoff_input - handoff_summary_cost
        task_passed = all(row["status"] == "passed" for row in task_rows)
        lines.append(
            f"| `{task_id}` | {'通过' if task_passed else '失败'} | "
            f"{_format_number(baseline_input)} | {_format_number(deterministic_input)} | "
            f"{_format_number(handoff_input)} | {_format_number(deterministic_net)} | "
            f"{_format_number(handoff_summary_cost)} | {_format_number(handoff_net)} |"
        )
    lines.extend(
        [
            "",
            "## 可复现性与审计入口",
            "",
            (
                "- 确定性校验会逐任务、逐变体比较两次运行的状态、输入 token、"
                "模型调用数、摘要调用成本、摘要调用标记和 verifier 返回码。"
            ),
            f"- 本次确定性失败项：{len(payload['determinism']['failures'])}。",
            (
                "- 原始数据见 `results.json`；每次模型请求、上下文压力决策、摘要 usage 与 "
                "verifier 状态见 `traces/<task>/<variant>/<repeat>.jsonl`。"
            ),
            "",
            (
                "要验证真实成本优化，应在 live 模式下记录 provider `actual` usage，"
                "并在相同任务、策略和重复次数下复测质量与净收益。"
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _median_field(rows: list[dict[str, Any]], field: str) -> float:
    return statistics.median(int(row[field]) for row in rows) if rows else 0


def _format_number(value: float | int) -> str:
    number = float(value)
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.1f}"
