from __future__ import annotations

import asyncio
from io import StringIO

from rich.console import Console

from paicli.entrypoints.repl import _run_plan_agent
from paicli.plan import (
    ExecutionPlan,
    JsonPlanner,
    PlanAndExecuteAgent,
    PlanExecutor,
    PlanReviewDecision,
    PlanTask,
    parse_plan_review_input,
)
from paicli.render import RichRenderer


def test_plan_executor_runs_tasks_after_dependencies():
    plan = ExecutionPlan(
        tasks=[
            PlanTask(id="inspect", description="Inspect files"),
            PlanTask(id="change", description="Change code", depends_on=["inspect"]),
            PlanTask(id="verify", description="Run tests", depends_on=["change"]),
        ]
    )
    calls: list[str] = []

    async def run_task(task: PlanTask) -> str:
        calls.append(task.id)
        return f"{task.id}:ok"

    async def run():
        executor = PlanExecutor()
        return [event async for event in executor.execute(plan, run_task)]

    events = asyncio.run(run())

    assert calls == ["inspect", "change", "verify"]
    assert [event["type"] for event in events] == [
        "plan_started",
        "task_started",
        "task_completed",
        "task_started",
        "task_completed",
        "task_started",
        "task_completed",
        "plan_completed",
    ]
    assert events[-1]["results"] == {
        "inspect": "inspect:ok",
        "change": "change:ok",
        "verify": "verify:ok",
    }


def test_plan_executor_skips_tasks_with_failed_dependencies():
    plan = ExecutionPlan(
        tasks=[
            PlanTask(id="inspect", description="Inspect files"),
            PlanTask(id="change", description="Change code", depends_on=["inspect"]),
            PlanTask(id="verify", description="Run tests", depends_on=["change"]),
        ]
    )

    async def run_task(task: PlanTask) -> str:
        if task.id == "change":
            raise RuntimeError("boom")
        return "ok"

    async def run():
        executor = PlanExecutor()
        return [event async for event in executor.execute(plan, run_task)]

    events = asyncio.run(run())

    assert any(event["type"] == "task_failed" and event["task_id"] == "change" for event in events)
    assert any(event["type"] == "task_skipped" and event["task_id"] == "verify" for event in events)
    assert events[-1]["type"] == "plan_failed"


def test_json_planner_parses_fenced_plan():
    raw = """
Here is the plan:

```json
{
  "tasks": [
    {"id": "inspect", "description": "Inspect files"},
    {"id": "change", "description": "Change code", "depends_on": ["inspect"]}
  ]
}
```
"""

    plan = JsonPlanner.parse(raw, goal="improve project")

    assert plan.goal == "improve project"
    assert plan.tasks == [
        PlanTask(id="inspect", description="Inspect files"),
        PlanTask(id="change", description="Change code", depends_on=["inspect"]),
    ]


def test_plan_review_input_parser_maps_commands():
    assert parse_plan_review_input("").action == "execute"
    assert parse_plan_review_input("yes").action == "execute"
    assert parse_plan_review_input("/run").action == "execute"
    assert parse_plan_review_input("\x0f").action == "expand"
    assert parse_plan_review_input("/view").action == "expand"
    assert parse_plan_review_input("\x1b").action == "cancel"
    assert parse_plan_review_input("\x1b", expanded=True).action == "collapse"
    assert parse_plan_review_input("/cancel").action == "cancel"

    supplement = parse_plan_review_input("also add tests")

    assert supplement == PlanReviewDecision.supplement("also add tests")


def test_execution_plan_summary_and_visualization():
    plan = ExecutionPlan(
        goal="ship it",
        tasks=[
            PlanTask(id="inspect", description="Inspect files"),
            PlanTask(id="change", description="Change code", depends_on=["inspect"]),
            PlanTask(id="verify", description="Run tests", depends_on=["change"]),
        ],
    )

    summary = plan.summary()
    visualization = plan.visualize()

    assert "ship it" in summary
    assert "任务数: 3" in summary
    assert "并行批次: 3" in summary
    assert "当前可执行: 1" in summary
    assert "首批执行: inspect" in summary
    assert "最终验收: verify" in summary
    assert "inspect [agent] deps=-: Inspect files" in visualization
    assert "change [agent] deps=inspect: Change code" in visualization


def test_plan_and_execute_agent_runs_planned_tasks_with_prior_results():
    class FixedPlanner:
        async def create_plan(self, goal: str) -> ExecutionPlan:
            return ExecutionPlan(
                goal=goal,
                tasks=[
                    PlanTask(id="inspect", description="Inspect files"),
                    PlanTask(id="change", description="Change code", depends_on=["inspect"]),
                ],
            )

    prompts: list[str] = []

    async def run_task(task: PlanTask, completed: dict[str, str]) -> str:
        prompts.append(task.description + "|" + "|".join(completed.values()))
        return task.id + ":done"

    async def run():
        agent = PlanAndExecuteAgent(planner=FixedPlanner(), task_runner=run_task)
        return [event async for event in agent.run("ship it")]

    events = asyncio.run(run())

    assert prompts == ["Inspect files|", "Change code|inspect:done"]
    assert events[0]["type"] == "plan_started"
    assert events[-1]["type"] == "plan_completed"


def test_run_plan_agent_cancel_does_not_execute_tasks():
    agent = FakeAgent(['{"tasks": [{"id": "inspect", "description": "Inspect files"}]}'])
    stream = StringIO()
    renderer = RichRenderer(console=Console(file=stream, color_system=None, width=120))

    async def review(_plan: ExecutionPlan, _expanded: bool) -> PlanReviewDecision:
        return PlanReviewDecision.cancel()

    asyncio.run(_run_plan_agent(agent, renderer, "ship it", review_input=review))

    assert agent.run_prompts == []
    assert "计划已取消" in stream.getvalue()


def test_run_plan_agent_supplement_replans_before_execute():
    agent = FakeAgent(
        [
            '{"tasks": [{"id": "inspect", "description": "Inspect files"}]}',
            (
                '{"tasks": ['
                '{"id": "inspect", "description": "Inspect files"},'
                '{"id": "verify", "description": "Run tests", "depends_on": ["inspect"]}'
                "]}"
            ),
        ]
    )
    stream = StringIO()
    renderer = RichRenderer(console=Console(file=stream, color_system=None, width=120))
    decisions = [
        PlanReviewDecision.supplement("add tests"),
        PlanReviewDecision.execute(),
    ]
    goals: list[str] = []

    async def review(plan: ExecutionPlan, _expanded: bool) -> PlanReviewDecision:
        goals.append(plan.goal)
        return decisions.pop(0)

    asyncio.run(_run_plan_agent(agent, renderer, "ship it", review_input=review))

    assert goals == ["ship it", "ship it\n补充要求：add tests"]
    assert len(agent.run_prompts) == 2
    assert "verify" in agent.run_prompts[1]


def test_run_plan_agent_expand_then_execute():
    agent = FakeAgent(['{"tasks": [{"id": "inspect", "description": "Inspect files"}]}'])
    stream = StringIO()
    renderer = RichRenderer(console=Console(file=stream, color_system=None, width=120))
    decisions = [PlanReviewDecision.expand(), PlanReviewDecision.execute()]

    async def review(_plan: ExecutionPlan, expanded: bool) -> PlanReviewDecision:
        if expanded:
            assert decisions[0].action == "execute"
        return decisions.pop(0)

    asyncio.run(_run_plan_agent(agent, renderer, "ship it", review_input=review))

    output = stream.getvalue()
    assert "完整计划" in output
    assert agent.run_prompts


class FakeAgent:
    def __init__(self, plan_json: list[str]):
        self.llm_client = FakeLlm(plan_json)
        self.run_prompts: list[str] = []

    async def run_complete(self, prompt: str):
        self.run_prompts.append(prompt)
        return FakeRunResult("ok")


class FakeRunResult:
    def __init__(self, text: str):
        self.text = text


class FakeLlm:
    provider_name = "fake"
    model_name = "fake"
    max_context_window = 1000

    def __init__(self, plan_json: list[str]):
        self.plan_json = list(plan_json)

    async def chat(self, _messages, _tools, *, system_prompt: str = ""):
        _ = system_prompt
        yield {"type": "thinking_delta", "thinking": "think first"}
        yield {"type": "text_delta", "text": self.plan_json.pop(0)}
