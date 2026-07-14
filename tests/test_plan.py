from __future__ import annotations

import asyncio
from io import StringIO

import pytest
from rich.console import Console

from paicli.entrypoints.repl import _run_plan_agent
from paicli.plan import (
    ExecutionPlan,
    JsonPlanner,
    PlanAndExecuteAgent,
    PlanExecutor,
    PlanReviewDecision,
    PlanStatus,
    PlanTask,
    TaskStatus,
    TaskType,
    build_task_context,
    build_task_system_prompt,
    create_minimal_plan,
    is_simple_goal,
    parse_plan_review_input,
)
from paicli.render import RichRenderer

# ---------------------------------------------------------------------------
# Existing tests (updated for new API)
# ---------------------------------------------------------------------------


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
    assert len(plan.tasks) == 2
    assert plan.tasks[0].id == "inspect"
    assert plan.tasks[0].description == "Inspect files"
    assert plan.tasks[1].id == "change"
    assert plan.tasks[1].depends_on == ["inspect"]


def test_plan_review_input_parser_maps_commands():
    assert parse_plan_review_input("").action == "execute"
    assert parse_plan_review_input("yes").action == "execute"
    assert parse_plan_review_input("/run").action == "execute"
    assert parse_plan_review_input("\x0f").action == "expand"
    assert parse_plan_review_input("\x0f", expanded=True).action == "collapse"
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
    assert "inspect [COMMAND] deps=-: Inspect files" in visualization
    assert "change [COMMAND] deps=inspect: Change code" in visualization


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
    reviewed_plans: list[ExecutionPlan] = []

    async def review(plan: ExecutionPlan, _expanded: bool) -> PlanReviewDecision:
        reviewed_plans.append(plan)
        return PlanReviewDecision.cancel()

    asyncio.run(_run_plan_agent(agent, renderer, "ship it", review_input=review))

    assert agent.run_prompts == []
    assert reviewed_plans[0].status == PlanStatus.CANCELLED
    assert "已取消" in stream.getvalue()


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


def test_run_plan_agent_keeps_task_history_isolated_and_records_summary():
    agent = FakeAgent(['{"tasks": [{"id": "inspect", "description": "Inspect files"}]}'])
    renderer = RichRenderer(console=Console(file=StringIO(), color_system=None, width=120))

    async def review(_plan: ExecutionPlan, _expanded: bool) -> PlanReviewDecision:
        return PlanReviewDecision.execute()

    asyncio.run(_run_plan_agent(agent, renderer, "ship it", review_input=review))

    assert agent.run_commit_history == [False]
    assert [message.role for message in agent.history] == ["user", "assistant"]
    assert agent.history[0].content == "ship it"
    assert "Plan execution summary:" in agent.history[1].content
    assert "inspect [COMPLETED]: Inspect files" in agent.history[1].content
    assert "Result: ok" in agent.history[1].content


def test_run_plan_agent_updates_renderer_usage_for_planning_and_tasks():
    class UsageLlm:
        provider_name = "fake"
        model_name = "fake"
        max_context_window = 1_000

        async def chat(self, _messages, _tools, *, system_prompt=""):
            yield {
                "type": "text_delta",
                "text": '{"tasks": [{"id": "inspect", "description": "Inspect files"}]}',
            }
            yield {
                "type": "usage",
                "usage": {"input_tokens": 11, "output_tokens": 7, "cached_tokens": 5},
            }

    class UsageAgent:
        llm_client = UsageLlm()
        cwd = "/tmp/fake"

        async def run(self, _prompt, *, commit_history=True):
            _ = commit_history
            yield {
                "type": "usage",
                "usage": {"input_tokens": 13, "output_tokens": 17, "cached_tokens": 3},
            }
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "done", "total_tokens": 30, "total_turns": 1}

    async def review(_plan, _expanded):
        return PlanReviewDecision.execute()

    renderer = RichRenderer(console=Console(file=StringIO(), color_system=None, width=120))
    asyncio.run(
        _run_plan_agent(
            UsageAgent(),
            renderer,
            "先检查代码并实现功能然后运行测试",
            review_input=review,
        )
    )

    status = renderer.toolbar_status()
    assert status["input_tokens"] == 13
    assert status["output_tokens"] == 17
    assert status["cached_tokens"] == 3
    assert status["context_ratio"] == pytest.approx(0.024)
    assert status["has_usage"] is True


# ---------------------------------------------------------------------------
# New tests for enhanced features
# ---------------------------------------------------------------------------


def test_compute_execution_order_detects_cycle():
    """Cycle detection should reject circular dependencies."""
    plan = ExecutionPlan(
        goal="cycle test",
        tasks=[
            PlanTask(id="a", description="A", depends_on=["b"]),
            PlanTask(id="b", description="B", depends_on=["a"]),
        ],
    )
    assert plan.compute_execution_order() is False


def test_compute_execution_order_valid_dag():
    """Valid DAG should pass cycle detection."""
    plan = ExecutionPlan(
        tasks=[
            PlanTask(id="a", description="A"),
            PlanTask(id="b", description="B", depends_on=["a"]),
            PlanTask(id="c", description="C", depends_on=["a", "b"]),
        ],
    )
    assert plan.compute_execution_order() is True


def test_validate_plan_raises_on_cycle():
    """_validate_plan should raise ValueError on cycles."""
    plan = ExecutionPlan(
        tasks=[
            PlanTask(id="x", description="X", depends_on=["y"]),
            PlanTask(id="y", description="Y", depends_on=["x"]),
        ],
    )
    with pytest.raises(ValueError, match="cycle"):
        plan.summary()  # triggers _validate_plan


def test_simple_goal_creates_minimal_plan_without_llm():
    """Simple goals should bypass LLM and create single-task plans."""
    plan = create_minimal_plan("列出当前目录的文件")
    assert len(plan.tasks) == 1
    assert plan.tasks[0].id == "task_1"
    assert plan.tasks[0].description == "列出当前目录的文件"
    assert plan.tasks[0].type == TaskType.FILE_READ


def test_simple_goal_detection():
    """Test is_simple_goal classification."""
    assert is_simple_goal("列出当前目录的文件") is True
    assert is_simple_goal("读取 pom.xml") is True
    assert is_simple_goal("查看项目结构") is True

    # Multi-step cues should make it complex
    assert is_simple_goal("先读取文件然后修改代码") is False
    assert is_simple_goal("查看配置并运行测试") is False
    assert is_simple_goal("在work文件夹下实现一个学生管理系统") is False

    # Long goals are complex
    assert is_simple_goal("这是一个非常非常长的任务描述需要多个步骤来完成并且需要验证结果") is False


def test_task_type_inference():
    """Test that create_minimal_plan infers correct TaskType."""
    plan = create_minimal_plan("列出所有Python文件")
    assert plan.tasks[0].type == TaskType.FILE_READ

    plan = create_minimal_plan("执行 pytest 测试")
    assert plan.tasks[0].type == TaskType.COMMAND


def test_complex_goal_calls_llm():
    """Complex goals should invoke LLM for planning."""

    class TrackingLlm:
        provider_name = "fake"
        model_name = "fake"
        max_context_window = 1000
        called = False

        async def chat(self, _messages, _tools, *, system_prompt=""):
            TrackingLlm.called = True
            yield {
                "type": "text_delta",
                "text": '{"tasks": [{"id": "t1", "description": "step 1"}]}',
            }

    llm = TrackingLlm()

    async def run():
        planner = JsonPlanner(llm)
        return await planner.create_plan("先读取配置然后修改代码并验证结果")

    plan = asyncio.run(run())
    assert llm.called is True
    assert len(plan.tasks) == 1


def test_simple_goal_does_not_call_llm():
    """Simple goals should NOT invoke LLM."""

    class FailingLlm:
        provider_name = "fake"
        model_name = "fake"
        max_context_window = 1000

        async def chat(self, _messages, _tools, *, system_prompt=""):
            raise AssertionError("LLM should not be called for simple goals")
            yield  # make it an async generator  # noqa: E501

    async def run():
        planner = JsonPlanner(FailingLlm())
        return await planner.create_plan("列出当前目录的文件")

    plan = asyncio.run(run())
    assert len(plan.tasks) == 1
    assert plan.tasks[0].type == TaskType.FILE_READ


def test_parallel_batch_execution():
    """Tasks with no inter-dependencies should execute in parallel."""
    plan = ExecutionPlan(
        goal="parallel test",
        tasks=[
            PlanTask(id="t1", description="Task 1"),
            PlanTask(id="t2", description="Task 2"),
            PlanTask(id="t3", description="Task 3", depends_on=["t1", "t2"]),
        ],
    )
    execution_order: list[str] = []

    async def run_task(task: PlanTask) -> str:
        execution_order.append(task.id)
        await asyncio.sleep(0.01)  # small delay to verify parallelism
        return f"{task.id}:ok"

    async def run():
        executor = PlanExecutor()
        return [event async for event in executor.execute(plan, run_task)]

    events = asyncio.run(run())

    # t1 and t2 should both start before t3
    started = [
        e["task_id"] for e in events if e["type"] == "task_started"
    ]
    assert started[:2] in (["t1", "t2"], ["t2", "t1"])
    assert started[2] == "t3"
    assert events[-1]["type"] == "plan_completed"


def test_task_status_transitions():
    """Test PlanTask state transition methods."""
    task = PlanTask(id="test", description="test task")
    assert task.status == TaskStatus.PENDING

    task.mark_started()
    assert task.status == TaskStatus.RUNNING
    assert task.start_time is not None

    task.mark_completed("result text")
    assert task.status == TaskStatus.COMPLETED
    assert task.result == "result text"
    assert task.end_time is not None
    assert task.duration is not None
    assert task.duration >= 0


def test_task_status_failed_and_skipped():
    """Test failed and skipped status transitions."""
    task = PlanTask(id="test", description="test")
    task.mark_failed("some error")
    assert task.status == TaskStatus.FAILED
    assert task.error == "some error"

    task2 = PlanTask(id="test2", description="test2")
    task2.mark_skipped()
    assert task2.status == TaskStatus.SKIPPED


def test_planner_injects_project_memory():
    """Project memory should be included in the planner system prompt."""
    captured_system: list[str] = []

    class CaptureLlm:
        provider_name = "fake"
        model_name = "fake"
        max_context_window = 1000

        async def chat(self, _messages, _tools, *, system_prompt=""):
            captured_system.append(system_prompt)
            yield {
                "type": "text_delta",
                "text": '{"tasks": [{"id": "t1", "description": "step"}]}',
            }

    async def run():
        planner = JsonPlanner(CaptureLlm(), project_memory="# My Project\nSome context")
        await planner.create_plan("complex task that needs planning and verification steps")

    asyncio.run(run())
    assert len(captured_system) == 1
    assert "# My Project" in captured_system[0]


def test_visualize_shows_status_icons():
    """Visualize should show status icons for each task."""
    plan = ExecutionPlan(
        goal="status test",
        tasks=[
            PlanTask(id="t1", description="Pending task"),
            PlanTask(id="t2", description="Completed task", status=TaskStatus.COMPLETED),
            PlanTask(id="t3", description="Failed task", status=TaskStatus.FAILED),
        ],
    )
    viz = plan.visualize()
    assert "⏳" in viz  # PENDING
    assert "✅" in viz  # COMPLETED
    assert "❌" in viz  # FAILED


def test_execution_plan_progress_ratio():
    """Test progress calculation."""
    plan = ExecutionPlan(
        tasks=[
            PlanTask(id="t1", description="done", status=TaskStatus.COMPLETED),
            PlanTask(id="t2", description="failed", status=TaskStatus.FAILED),
            PlanTask(id="t3", description="pending"),
            PlanTask(id="t4", description="skipped", status=TaskStatus.SKIPPED),
        ],
    )
    assert plan.progress_ratio == pytest.approx(0.75)


def test_execution_plan_leaf_tasks():
    """Test leaf task identification (tasks with no dependents)."""
    plan = ExecutionPlan(
        tasks=[
            PlanTask(id="t1", description="root"),
            PlanTask(id="t2", description="middle", depends_on=["t1"]),
            PlanTask(id="t3", description="leaf", depends_on=["t2"]),
        ],
    )
    leaf_ids = [t.id for t in plan.leaf_tasks]
    assert leaf_ids == ["t3"]


def test_build_task_context():
    """Test task context building with dependency results."""
    plan = ExecutionPlan(
        goal="test goal",
        tasks=[
            PlanTask(
                id="t1", description="first task",
                status=TaskStatus.COMPLETED, result="file contents here",
            ),
            PlanTask(id="t2", description="second task", depends_on=["t1"]),
        ],
    )
    context = build_task_context(plan, plan.tasks[1])
    assert "test goal" in context
    assert "second task" in context
    assert "first task" in context
    assert "COMPLETED" in context
    assert "file contents here" in context


def test_build_task_system_prompt():
    """Test per-task system prompt generation."""
    task = PlanTask(
        id="t1",
        description="Read config file",
        type=TaskType.FILE_READ,
    )
    prompt = build_task_system_prompt(task)
    assert "FILE_READ" in prompt
    assert "Read config file" in prompt


def test_json_planner_parses_task_types():
    """Parser should handle task type field."""
    raw = '{"tasks": [{"id": "t1", "description": "read file", "type": "FILE_READ"}]}'
    plan = JsonPlanner.parse(raw, goal="test")
    assert plan.tasks[0].type == TaskType.FILE_READ


def test_json_planner_normalizes_ids():
    """Parser should normalize task IDs."""
    raw = (
        '{"tasks": ['
        '{"id": "inspect", "description": "look"},'
        '{"id": "change", "description": "edit", "depends_on": ["inspect"]}'
        ']}'
    )
    plan = JsonPlanner.parse(raw, goal="test")
    assert plan.tasks[0].id == "inspect"
    assert plan.tasks[1].depends_on == ["inspect"]


def test_get_executable_tasks():
    """Test filtering executable tasks based on dependency completion."""
    plan = ExecutionPlan(
        tasks=[
            PlanTask(id="a", description="A", status=TaskStatus.COMPLETED),
            PlanTask(id="b", description="B", depends_on=["a"]),
            PlanTask(id="c", description="C", depends_on=["a", "b"]),
        ],
    )
    executable = plan.get_executable_tasks()
    assert len(executable) == 1
    assert executable[0].id == "b"


def test_plan_replan():
    """Test planner replan generates augmented goal."""

    class CaptureLlm:
        provider_name = "fake"
        model_name = "fake"
        max_context_window = 1000
        last_goal = ""

        async def chat(self, messages, _tools, *, system_prompt=""):
            CaptureLlm.last_goal = messages[0].content
            yield {
                "type": "text_delta",
                "text": '{"tasks": [{"id": "retry", "description": "retry task"}]}',
            }

    llm = CaptureLlm()

    async def run():
        planner = JsonPlanner(llm)
        return await planner.replan(
            "original goal",
            "task_2 failed: timeout",
            {"task_1": "completed result"},
        )

    plan = asyncio.run(run())
    assert len(plan.tasks) == 1
    assert "original goal" in CaptureLlm.last_goal
    assert "timeout" in CaptureLlm.last_goal


def test_event_sink_receives_events():
    """PlanExecutor should forward events through event_sink."""
    plan = ExecutionPlan(
        tasks=[PlanTask(id="t1", description="test")],
    )
    sink_events: list[dict] = []

    def sink(event: dict) -> None:
        sink_events.append(event)

    async def run_task(task: PlanTask, completed: dict, *, event_sink=None) -> str:
        if event_sink:
            event_sink({"type": "task_text_delta", "task_id": "t1", "text": "hello"})
        return "done"

    async def run():
        executor = PlanExecutor()
        return [event async for event in executor.execute(plan, run_task, event_sink=sink)]

    asyncio.run(run())

    assert len(sink_events) == 1
    assert sink_events[0]["type"] == "task_text_delta"
    assert sink_events[0]["text"] == "hello"


def test_task_duration_tracking():
    """Test that task duration is tracked after completion."""
    plan = ExecutionPlan(
        tasks=[PlanTask(id="t1", description="timed task")],
    )

    async def run_task(task: PlanTask) -> str:
        await asyncio.sleep(0.05)
        return "done"

    async def run():
        executor = PlanExecutor()
        return [event async for event in executor.execute(plan, run_task)]

    events = asyncio.run(run())

    completed_event = next(e for e in events if e["type"] == "task_completed")
    assert completed_event.get("duration") is not None
    assert completed_event["duration"] >= 0.04


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class FakeAgent:
    def __init__(self, plan_json: list[str]):
        self.llm_client = FakeLlm(plan_json)
        self.run_prompts: list[str] = []
        self.run_commit_history: list[bool] = []
        self.history = []
        self.cwd = "/tmp/fake"

    async def run(self, prompt: str, *, commit_history: bool = True):
        """Streaming agent run for plan tasks."""
        self.run_prompts.append(prompt)
        self.run_commit_history.append(commit_history)
        yield {"type": "text_delta", "text": "ok"}
        yield {"type": "done", "total_tokens": 0, "total_turns": 1, "messages": []}

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
