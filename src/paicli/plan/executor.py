from __future__ import annotations

import inspect
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from paicli.llm.base import LlmClient
from paicli.types import Message


@dataclass(slots=True)
class PlanTask:
    id: str
    description: str
    kind: str = "agent"
    depends_on: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ExecutionPlan:
    tasks: list[PlanTask]
    goal: str = ""

    def summary(self) -> str:
        tasks = _validate_plan(self)
        batches = _dependency_batches(tasks)
        ready = [task.id for task in tasks if not task.depends_on]
        final = _final_task_ids(tasks)
        lines = [
            "计划摘要",
            f"- 目标: {self.goal or '(未指定)'}",
            (
                f"- 任务数: {len(tasks)} | 并行批次: {len(batches)} | "
                f"当前可执行: {len(ready)} | 状态: CREATED"
            ),
            f"- 首批执行: {', '.join(ready) if ready else '-'}",
            f"- 最终验收: {', '.join(final) if final else '-'}",
        ]
        return "\n".join(lines)

    def visualize(self) -> str:
        tasks = _validate_plan(self)
        lines = [f"完整计划: {self.goal or '(未指定)'}"]
        for task in tasks:
            depends_on = ", ".join(task.depends_on) if task.depends_on else "-"
            lines.append(f"- {task.id} [{task.kind}] deps={depends_on}: {task.description}")
        return "\n".join(lines)


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


TaskRunner = Callable[..., Awaitable[str]]


class PlanExecutor:
    async def execute(
        self,
        plan: ExecutionPlan,
        run_task: TaskRunner,
    ) -> AsyncIterator[dict[str, Any]]:
        tasks = _validate_plan(plan)
        completed: dict[str, str] = {}
        failed: dict[str, str] = {}
        skipped: set[str] = set()

        yield {
            "type": "plan_started",
            "goal": plan.goal,
            "tasks": [_task_payload(task) for task in tasks],
        }

        remaining = list(tasks)
        while remaining:
            ready = [
                task
                for task in remaining
                if all(
                    dependency in completed or dependency in failed or dependency in skipped
                    for dependency in task.depends_on
                )
            ]
            if not ready:
                unresolved = ", ".join(task.id for task in remaining)
                yield {"type": "plan_failed", "error": f"unresolved dependencies: {unresolved}"}
                return

            for task in ready:
                remaining.remove(task)
                failed_dependencies = [
                    dependency
                    for dependency in task.depends_on
                    if dependency in failed or dependency in skipped
                ]
                if failed_dependencies:
                    skipped.add(task.id)
                    yield {
                        "type": "task_skipped",
                        "task_id": task.id,
                        "dependencies": failed_dependencies,
                    }
                    continue

                yield {"type": "task_started", "task_id": task.id, "task": _task_payload(task)}
                try:
                    result = await _call_task_runner(run_task, task, completed)
                except Exception as exc:  # noqa: BLE001 - plan execution reports task failures
                    failed[task.id] = str(exc)
                    yield {"type": "task_failed", "task_id": task.id, "error": str(exc)}
                    continue

                completed[task.id] = result
                yield {"type": "task_completed", "task_id": task.id, "result": result}

        if failed or skipped:
            yield {"type": "plan_failed", "results": completed, "failed": failed}
            return
        yield {"type": "plan_completed", "results": completed}


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
    return list(plan.tasks)


def parse_plan_review_input(raw: str, *, expanded: bool = False) -> PlanReviewDecision:
    if raw == "\x0f":
        return PlanReviewDecision.expand()
    if raw == "\x1b":
        return PlanReviewDecision.collapse() if expanded else PlanReviewDecision.cancel()

    text = raw.strip()
    normalized = text.lower()
    if normalized in {"", "y", "yes", "run", "/run"}:
        return PlanReviewDecision.execute()
    if normalized in {"cancel", "esc", "/cancel"}:
        return PlanReviewDecision.cancel()
    if normalized in {"view", "/view", "ctrl+o"}:
        return PlanReviewDecision.expand()
    if normalized in {"i", "/supplement", "supplement"}:
        return PlanReviewDecision.supplement()
    return PlanReviewDecision.supplement(text)


def _task_payload(task: PlanTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "description": task.description,
        "kind": task.kind,
        "depends_on": list(task.depends_on),
    }


async def _call_task_runner(
    run_task: TaskRunner,
    task: PlanTask,
    completed: dict[str, str],
) -> str:
    parameters = inspect.signature(run_task).parameters
    if len(parameters) >= 2:
        return await run_task(task, dict(completed))
    return await run_task(task)


class JsonPlanner:
    def __init__(self, llm_client: LlmClient | None = None):
        self.llm_client = llm_client
        self.last_raw_plan = ""
        self.last_thinking = ""

    async def create_plan(self, goal: str) -> ExecutionPlan:
        if not self.llm_client:
            raise ValueError("JsonPlanner needs an LLM client")
        text = ""
        thinking = ""
        messages = [
            Message(
                role="user",
                content=(
                    "Create a concise JSON execution plan for this task. "
                    "Return only JSON with a tasks array. Each task must have "
                    "id, description, and optional depends_on.\n\nTask:\n" + goal
                ),
            )
        ]
        async for event in self.llm_client.chat(messages, [], system_prompt=_PLANNER_SYSTEM):
            if event.get("type") == "text_delta":
                text += str(event.get("text") or "")
            elif event.get("type") == "thinking_delta":
                thinking += str(event.get("thinking") or event.get("text") or "")
            elif event.get("type") == "error":
                raise event["error"]
        self.last_raw_plan = text
        self.last_thinking = thinking
        return self.parse(text, goal=goal)

    @staticmethod
    def parse(raw: str, *, goal: str = "") -> ExecutionPlan:
        data = json.loads(_extract_json(raw))
        raw_tasks = data.get("tasks") if isinstance(data, dict) else None
        if not isinstance(raw_tasks, list):
            raise ValueError("plan JSON must contain a tasks array")
        tasks = []
        for index, raw_task in enumerate(raw_tasks, start=1):
            if not isinstance(raw_task, dict):
                raise ValueError("plan task must be an object")
            task_id = str(raw_task.get("id") or f"task_{index}")
            description = str(raw_task.get("description") or raw_task.get("task") or "").strip()
            if not description:
                raise ValueError(f"plan task {task_id} needs a description")
            depends_on = raw_task.get("depends_on") or raw_task.get("dependencies") or []
            if isinstance(depends_on, str):
                depends_on = [depends_on]
            tasks.append(
                PlanTask(
                    id=task_id,
                    description=description,
                    kind=str(raw_task.get("kind") or "agent"),
                    depends_on=[str(item) for item in depends_on],
                )
            )
        return ExecutionPlan(tasks=tasks, goal=goal)


class PlanAndExecuteAgent:
    def __init__(self, *, planner: Any, task_runner: TaskRunner):
        self.planner = planner
        self.task_runner = task_runner
        self.executor = PlanExecutor()

    async def run(self, goal: str) -> AsyncIterator[dict[str, Any]]:
        plan = await self.planner.create_plan(goal)
        async for event in self.executor.execute(plan, self.task_runner):
            yield event


def _extract_json(raw: str) -> str:
    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw, re.IGNORECASE)
    if fenced:
        return fenced.group(1)
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end >= start:
        return raw[start : end + 1]
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


_PLANNER_SYSTEM = (
    "You are PaiCLI's planner. Break work into a small dependency ordered DAG. "
    "Use stable ids like inspect, change, verify. Return valid JSON only."
)
