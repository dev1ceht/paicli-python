from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from paicli.llm.base import LlmClient
from paicli.types import Message


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TaskType(str, Enum):
    FILE_READ = "FILE_READ"
    FILE_WRITE = "FILE_WRITE"
    COMMAND = "COMMAND"
    ANALYSIS = "ANALYSIS"
    VERIFICATION = "VERIFICATION"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class PlanStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# ---------------------------------------------------------------------------
# Status icons
# ---------------------------------------------------------------------------

_STATUS_ICONS = {
    TaskStatus.PENDING: "⏳",
    TaskStatus.RUNNING: "▶️",
    TaskStatus.COMPLETED: "✅",
    TaskStatus.FAILED: "❌",
    TaskStatus.SKIPPED: "⏭️",
}

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PlanTask:
    id: str
    description: str
    kind: str = "agent"
    type: TaskType = TaskType.COMMAND
    depends_on: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    result: str | None = None
    error: str | None = None
    start_time: float | None = None
    end_time: float | None = None

    # -- state transitions --------------------------------------------------

    def mark_started(self) -> None:
        self.status = TaskStatus.RUNNING
        self.start_time = time.monotonic()

    def mark_completed(self, result: str) -> None:
        self.status = TaskStatus.COMPLETED
        self.result = result
        self.end_time = time.monotonic()

    def mark_failed(self, error: str) -> None:
        self.status = TaskStatus.FAILED
        self.error = error
        self.end_time = time.monotonic()

    def mark_skipped(self) -> None:
        self.status = TaskStatus.SKIPPED

    @property
    def duration(self) -> float | None:
        if self.start_time is not None and self.end_time is not None:
            return self.end_time - self.start_time
        return None

    def is_executable(self, all_tasks: dict[str, PlanTask]) -> bool:
        if self.status != TaskStatus.PENDING:
            return False
        return all(
            all_tasks[dep_id].status == TaskStatus.COMPLETED
            for dep_id in self.depends_on
            if dep_id in all_tasks
        )


@dataclass(slots=True)
class ExecutionPlan:
    tasks: list[PlanTask]
    goal: str = ""
    status: PlanStatus = PlanStatus.CREATED

    def summary(self) -> str:
        tasks = _validate_plan(self)
        batches = _dependency_batches(tasks)
        ready = [task.id for task in tasks if not task.depends_on]
        final = _final_task_ids(tasks)

        status_counts: dict[str, int] = {}
        for task in tasks:
            status_counts[task.status.value] = status_counts.get(task.status.value, 0) + 1
        status_line = " ".join(f"{k}={v}" for k, v in status_counts.items() if v > 0)

        lines = [
            "计划摘要",
            f"- 目标: {self.goal or '(未指定)'}",
            (
                f"- 任务数: {len(tasks)} | 并行批次: {len(batches)} | "
                f"当前可执行: {len(ready)} | 状态: {self.status.value}"
            ),
        ]
        if status_line:
            lines.append(f"- 任务状态: {status_line}")
        lines.append(f"- 首批执行: {', '.join(ready) if ready else '-'}")
        lines.append(f"- 最终验收: {', '.join(final) if final else '-'}")
        return "\n".join(lines)

    def visualize(self) -> str:
        tasks = _validate_plan(self)
        lines = [f"完整计划: {self.goal or '(未指定)'}"]
        for task in tasks:
            icon = _STATUS_ICONS.get(task.status, "")
            depends_on = ", ".join(task.depends_on) if task.depends_on else "-"
            lines.append(
                f"{icon} {task.id} [{task.type.value}] deps={depends_on}: "
                f"{task.description}"
            )
        return "\n".join(lines)

    def compute_execution_order(self) -> bool:
        """Topological sort with cycle detection. Returns False if a cycle is found."""
        task_map = {task.id: task for task in self.tasks}
        visited: set[str] = set()
        visiting: set[str] = set()
        order: list[str] = []

        def _dfs(task_id: str) -> bool:
            if task_id in visited:
                return True
            if task_id in visiting:
                return False  # cycle
            visiting.add(task_id)
            task = task_map.get(task_id)
            if task:
                for dep_id in task.depends_on:
                    if not _dfs(dep_id):
                        return False
            visiting.discard(task_id)
            visited.add(task_id)
            order.append(task_id)
            return True

        for task in self.tasks:
            if not _dfs(task.id):
                return False
        return True

    def get_task(self, task_id: str) -> PlanTask | None:
        for task in self.tasks:
            if task.id == task_id:
                return task
        return None

    def get_executable_tasks(self) -> list[PlanTask]:
        task_map = {task.id: task for task in self.tasks}
        return [task for task in self.tasks if task.is_executable(task_map)]

    @property
    def progress_ratio(self) -> float:
        if not self.tasks:
            return 0.0
        done = sum(
            1 for task in self.tasks
            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SKIPPED)
        )
        return done / len(self.tasks)

    @property
    def leaf_tasks(self) -> list[PlanTask]:
        dep_ids = {dep_id for task in self.tasks for dep_id in task.depends_on}
        return [task for task in self.tasks if task.id not in dep_ids]


# ---------------------------------------------------------------------------
# Review decisions
# ---------------------------------------------------------------------------

PlanReviewAction = Literal["execute", "supplement", "cancel", "expand", "collapse"]


@dataclass(frozen=True, slots=True)
class PlanReviewDecision:
    action: PlanReviewAction
    feedback: str = ""

    @classmethod
    def execute(cls) -> PlanReviewDecision:
        return cls("execute")

    @classmethod
    def supplement(cls, feedback: str = "") -> PlanReviewDecision:
        return cls("supplement", feedback)

    @classmethod
    def cancel(cls) -> PlanReviewDecision:
        return cls("cancel")

    @classmethod
    def expand(cls) -> PlanReviewDecision:
        return cls("expand")

    @classmethod
    def collapse(cls) -> PlanReviewDecision:
        return cls("collapse")


def parse_plan_review_input(raw: str, *, expanded: bool = False) -> PlanReviewDecision:
    if raw == "\x0f":
        return PlanReviewDecision.collapse() if expanded else PlanReviewDecision.expand()
    if raw == "\x1b":
        return PlanReviewDecision.collapse() if expanded else PlanReviewDecision.cancel()

    text = raw.strip()
    normalized = text.lower()
    if normalized in {"", "y", "yes", "run", "/run"}:
        return PlanReviewDecision.execute()
    if normalized in {"cancel", "esc", "/cancel"}:
        return PlanReviewDecision.cancel()
    if normalized == "ctrl+o":
        return PlanReviewDecision.collapse() if expanded else PlanReviewDecision.expand()
    if normalized in {"view", "/view"}:
        return PlanReviewDecision.expand()
    if normalized in {"i", "/supplement", "supplement"}:
        return PlanReviewDecision.supplement()
    return PlanReviewDecision.supplement(text)


# ---------------------------------------------------------------------------
# Task runner types
# ---------------------------------------------------------------------------

TaskRunner = Callable[..., Awaitable[str]]
EventSink = Callable[[dict[str, Any]], None]


# ---------------------------------------------------------------------------
# PlanExecutor
# ---------------------------------------------------------------------------


class PlanExecutor:
    def __init__(self, *, max_parallel: int = 4):
        self._max_parallel = max_parallel

    async def execute(
        self,
        plan: ExecutionPlan,
        run_task: TaskRunner,
        *,
        event_sink: EventSink | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        tasks = _validate_plan(plan)
        if not plan.compute_execution_order():
            yield {"type": "plan_failed", "error": "plan contains a dependency cycle"}
            return

        task_map = {task.id: task for task in tasks}
        completed: dict[str, str] = {}
        failed: dict[str, str] = {}
        skipped: set[str] = set()

        plan.status = PlanStatus.RUNNING
        yield {
            "type": "plan_started",
            "goal": plan.goal,
            "tasks": [_task_payload(task) for task in tasks],
        }

        remaining = list(tasks)
        semaphore = asyncio.Semaphore(self._max_parallel)

        while remaining:
            ready = [
                task
                for task in remaining
                if all(
                    dependency in completed
                    or dependency in failed
                    or dependency in skipped
                    for dependency in task.depends_on
                )
            ]
            if not ready:
                unresolved = ", ".join(task.id for task in remaining)
                plan.status = PlanStatus.FAILED
                yield {
                    "type": "plan_failed",
                    "error": f"unresolved dependencies: {unresolved}",
                }
                return

            if len(ready) == 1:
                task = ready[0]
                remaining.remove(task)
                failed_deps = [
                    d for d in task.depends_on if d in failed or d in skipped
                ]
                if failed_deps:
                    task.mark_skipped()
                    skipped.add(task.id)
                    yield {
                        "type": "task_skipped",
                        "task_id": task.id,
                        "dependencies": failed_deps,
                    }
                    continue

                yield {
                    "type": "task_started",
                    "task_id": task.id,
                    "task": _task_payload(task),
                }
                task.mark_started()
                try:
                    result = await _call_task_runner(
                        run_task, task, completed, event_sink=event_sink,
                    )
                except Exception as exc:
                    task.mark_failed(str(exc))
                    failed[task.id] = str(exc)
                    yield {
                        "type": "task_failed",
                        "task_id": task.id,
                        "error": str(exc),
                    }
                    continue

                task.mark_completed(result)
                completed[task.id] = result
                yield {
                    "type": "task_completed",
                    "task_id": task.id,
                    "result": result,
                    "duration": task.duration,
                }
            else:
                # -- parallel batch ------------------------------------------
                async def _run_one(t: PlanTask) -> tuple[PlanTask, str | None, str | None]:
                    async with semaphore:
                        failed_deps = [
                            d for d in t.depends_on if d in failed or d in skipped
                        ]
                        if failed_deps:
                            t.mark_skipped()
                            return (t, None, None)

                        t.mark_started()
                        try:
                            r = await _call_task_runner(
                                run_task, t, completed, event_sink=event_sink,
                            )
                            return (t, r, None)
                        except Exception as exc:
                            return (t, None, str(exc))

                for task in ready:
                    remaining.remove(task)
                    yield {
                        "type": "task_started",
                        "task_id": task.id,
                        "task": _task_payload(task),
                    }

                results = await asyncio.gather(*[_run_one(task) for task in ready])

                for task, result, error in results:
                    if error is not None:
                        task.mark_failed(error)
                        failed[task.id] = error
                        yield {
                            "type": "task_failed",
                            "task_id": task.id,
                            "error": error,
                        }
                    elif result is not None:
                        task.mark_completed(result)
                        completed[task.id] = result
                        yield {
                            "type": "task_completed",
                            "task_id": task.id,
                            "result": result,
                            "duration": task.duration,
                        }
                    elif task.status == TaskStatus.SKIPPED:
                        skipped.add(task.id)
                        failed_deps = [
                            d for d in task.depends_on if d in failed or d in skipped
                        ]
                        yield {
                            "type": "task_skipped",
                            "task_id": task.id,
                            "dependencies": failed_deps,
                        }

        if failed or skipped:
            plan.status = PlanStatus.FAILED
            yield {"type": "plan_failed", "results": completed, "failed": failed}
            return

        plan.status = PlanStatus.COMPLETED
        yield {"type": "plan_completed", "results": completed}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_plan(plan: ExecutionPlan) -> list[PlanTask]:
    seen: set[str] = set()
    for task in plan.tasks:
        if not task.id:
            raise ValueError("plan task id is required")
        if task.id in seen:
            raise ValueError(f"duplicate plan task id: {task.id}")
        seen.add(task.id)
    for task in plan.tasks:
        missing = [dependency for dependency in task.depends_on if dependency not in seen]
        if missing:
            raise ValueError(f"task {task.id} depends on unknown tasks: {', '.join(missing)}")
    # cycle detection
    if not plan.compute_execution_order():
        raise ValueError("plan contains a dependency cycle")
    return list(plan.tasks)


# ---------------------------------------------------------------------------
# JsonPlanner
# ---------------------------------------------------------------------------


class JsonPlanner:
    def __init__(
        self,
        llm_client: LlmClient | None = None,
        *,
        project_memory: str = "",
    ):
        self.llm_client = llm_client
        self.project_memory = project_memory
        self.last_raw_plan = ""
        self.last_thinking = ""

    async def create_plan(self, goal: str) -> ExecutionPlan:
        if is_simple_goal(goal):
            return create_minimal_plan(goal)

        if not self.llm_client:
            raise ValueError("JsonPlanner needs an LLM client")

        system_prompt = _PLANNER_SYSTEM
        if self.project_memory:
            system_prompt += f"\n\n项目上下文:\n{self.project_memory[:2000]}"

        text = ""
        thinking = ""
        messages = [
            Message(
                role="user",
                content=(
                    "Create a concise JSON execution plan for this task. "
                    "Return only JSON with a tasks array. Each task must have "
                    "id, description, type, and optional depends_on.\n\nTask:\n" + goal
                ),
            )
        ]
        async for event in self.llm_client.chat(messages, [], system_prompt=system_prompt):
            if event.get("type") == "text_delta":
                text += str(event.get("text") or "")
            elif event.get("type") == "thinking_delta":
                thinking += str(event.get("thinking") or event.get("text") or "")
            elif event.get("type") == "error":
                raise event["error"]

        self.last_raw_plan = text
        self.last_thinking = thinking
        return self.parse(text, goal=goal)

    async def replan(
        self,
        original_goal: str,
        failure_reason: str,
        completed_tasks: dict[str, str],
    ) -> ExecutionPlan:
        completed_summary = "\n".join(
            f"- {tid}: {result[:200]}" for tid, result in completed_tasks.items()
        )
        replan_goal = (
            f"原始目标: {original_goal}\n"
            f"失败原因: {failure_reason}\n"
            f"已完成任务:\n{completed_summary}\n"
            f"请基于以上信息重新规划剩余任务。"
        )
        return await self.create_plan(replan_goal)

    @staticmethod
    def parse(raw: str, *, goal: str = "") -> ExecutionPlan:
        data = json.loads(_extract_json(raw))
        raw_tasks = data.get("tasks") if isinstance(data, dict) else None
        if not isinstance(raw_tasks, list):
            raise ValueError("plan JSON must contain a tasks array")

        id_mapping: dict[str, str] = {}
        parsed: list[dict[str, Any]] = []

        for index, raw_task in enumerate(raw_tasks, start=1):
            if not isinstance(raw_task, dict):
                raise ValueError("plan task must be an object")
            original_id = str(raw_task.get("id") or f"task_{index}")
            normalized_id = f"task_{index}" if not raw_task.get("id") else original_id
            id_mapping[original_id] = normalized_id

            description = str(
                raw_task.get("description") or raw_task.get("task") or ""
            ).strip()
            if not description:
                raise ValueError(f"plan task {normalized_id} needs a description")

            raw_type = str(raw_task.get("type") or raw_task.get("kind") or "COMMAND").upper()
            try:
                task_type = TaskType(raw_type)
            except ValueError:
                task_type = TaskType.COMMAND

            parsed.append({
                "id": normalized_id,
                "description": description,
                "type": task_type,
                "raw_deps": raw_task.get("depends_on")
                or raw_task.get("dependencies")
                or [],
            })

        tasks: list[PlanTask] = []
        for item in parsed:
            raw_deps = item["raw_deps"]
            if isinstance(raw_deps, str):
                raw_deps = [raw_deps]
            depends_on = [
                id_mapping.get(str(d), str(d)) for d in raw_deps
            ]
            tasks.append(
                PlanTask(
                    id=item["id"],
                    description=item["description"],
                    kind="agent",
                    type=item["type"],
                    depends_on=depends_on,
                )
            )

        plan = ExecutionPlan(tasks=tasks, goal=goal)
        if not plan.compute_execution_order():
            raise ValueError("plan contains a dependency cycle")
        return plan


# ---------------------------------------------------------------------------
# PlanAndExecuteAgent (convenience wrapper)
# ---------------------------------------------------------------------------


class PlanAndExecuteAgent:
    def __init__(self, *, planner: Any, task_runner: TaskRunner):
        self.planner = planner
        self.task_runner = task_runner
        self.executor = PlanExecutor()

    async def run(self, goal: str) -> AsyncIterator[dict[str, Any]]:
        plan = await self.planner.create_plan(goal)
        async for event in self.executor.execute(plan, self.task_runner):
            yield event


# ---------------------------------------------------------------------------
# Simple goal detection
# ---------------------------------------------------------------------------

_MULTI_STEP_CUES = frozenset([
    "然后", "并且", "并", "再", "最后", "同时", "先", "之后", "接着", "以及",
])

_ACTION_WORDS = frozenset([
    "列出", "查看", "读取", "显示", "执行", "运行", "搜索",
    "当前目录", "文件",
])

_FILE_READ_WORDS = frozenset(["读取", "查看", "显示", "列出", "搜索", "文件"])
_COMMAND_WORDS = frozenset(["执行", "运行"])


def is_simple_goal(goal: str) -> bool:
    for cue in _MULTI_STEP_CUES:
        if cue in goal:
            return False
    if len(goal) > 30:
        return False
    return any(word in goal for word in _ACTION_WORDS)


def _infer_task_type(goal: str) -> TaskType:
    if any(word in goal for word in _FILE_READ_WORDS):
        return TaskType.FILE_READ
    if any(word in goal for word in _COMMAND_WORDS):
        return TaskType.COMMAND
    return TaskType.COMMAND


def create_minimal_plan(goal: str) -> ExecutionPlan:
    task_type = _infer_task_type(goal)
    task = PlanTask(
        id="task_1",
        description=goal,
        kind="agent",
        type=task_type,
    )
    return ExecutionPlan(
        tasks=[task],
        goal=goal,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task_payload(task: PlanTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "description": task.description,
        "kind": task.kind,
        "type": task.type.value,
        "depends_on": list(task.depends_on),
    }


async def _call_task_runner(
    run_task: TaskRunner,
    task: PlanTask,
    completed: dict[str, str],
    *,
    event_sink: EventSink | None = None,
) -> str:
    parameters = inspect.signature(run_task).parameters
    # Check if runner accepts event_sink as keyword
    if "event_sink" in parameters:
        return await run_task(task, dict(completed), event_sink=event_sink)  # type: ignore[call-arg]
    if len(parameters) >= 2:
        return await run_task(task, dict(completed))
    return await run_task(task)


def _extract_json(raw: str) -> str:
    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw, re.IGNORECASE)
    if fenced:
        return fenced.group(1)
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end >= start:
        return raw[start: end + 1]
    return raw


def _dependency_batches(tasks: list[PlanTask]) -> list[list[str]]:
    remaining = {task.id: task for task in tasks}
    completed: set[str] = set()
    batches: list[list[str]] = []
    while remaining:
        ready = [
            task_id
            for task_id, task in remaining.items()
            if all(dependency in completed for dependency in task.depends_on)
        ]
        if not ready:
            return batches
        batches.append(ready)
        completed.update(ready)
        for task_id in ready:
            del remaining[task_id]
    return batches


def _final_task_ids(tasks: list[PlanTask]) -> list[str]:
    dependencies = {
        dependency
        for task in tasks
        for dependency in task.depends_on
    }
    return [task.id for task in tasks if task.id not in dependencies]


def build_task_context(
    plan: ExecutionPlan,
    task: PlanTask,
) -> str:
    """Build the context string injected into a task's prompt."""
    parts = [f"总目标: {plan.goal}", f"当前任务: {task.description}"]
    if task.depends_on:
        parts.append("依赖任务结果:")
        for dep_id in task.depends_on:
            dep = plan.get_task(dep_id)
            if dep:
                parts.append(
                    f"- {dep.id} / {dep.description} / 状态={dep.status.value}"
                )
                if dep.result:
                    preview = dep.result[:4000]
                    parts.append(preview)
    else:
        parts.append("无依赖任务。")
    parts.append("请执行此任务并输出结果。")
    return "\n\n".join(parts)


def build_task_system_prompt(task: PlanTask) -> str:
    """Build the per-task system prompt."""
    return _TASK_SYSTEM.format(
        task_type=task.type.value,
        task_description=task.description,
    )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_PLANNER_SYSTEM = """\
你是任务规划专家。将复杂任务分解为可执行子任务。

任务类型: FILE_READ, FILE_WRITE, COMMAND, ANALYSIS, VERIFICATION

输出 JSON 格式:
{
  "tasks": [
    {
      "id": "task_1",
      "description": "任务描述",
      "type": "FILE_READ",
      "dependencies": []
    }
  ]
}

规则:
1. 唯一 id (task_1, task_2...)
2. dependencies 列出依赖的 task id
3. 按执行顺序排列
4. 描述具体明确
5. 简单任务 1-3 个步骤
6. 复杂任务 5-10 个步骤
7. 不要为保存中间结果额外创建 FILE_WRITE/FILE_READ
8. 一步能完成就保持最短计划

只输出 JSON"""

_TASK_SYSTEM = """\
你是 Plan-and-Execute 中的任务执行专家。
当前任务类型：{task_type}
任务描述：{task_description}
优先用 glob_files/grep_code/read_file 现用现查；
ANALYSIS/VERIFICATION 类型且上下文足够时直接输出结果。\
"""
