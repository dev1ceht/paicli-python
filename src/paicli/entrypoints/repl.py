from __future__ import annotations

import json
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from paicli import __version__
from paicli.agent import Agent
from paicli.bootstrap import build_tool_registry
from paicli.browser import BrowserSession
from paicli.config import PaiCliConfig, config_to_public_dict
from paicli.llm import create_llm_client
from paicli.mcp import McpClientManager
from paicli.memory import MemoryManager
from paicli.plan import (
    ExecutionPlan,
    JsonPlanner,
    PlanExecutor,
    PlanReviewDecision,
    PlanStatus,
    PlanTask,
    TaskStatus,
    build_task_context,
    build_task_system_prompt,
    parse_plan_review_input,
)
from paicli.policy import AuditLog
from paicli.prompt import PromptAssembler
from paicli.prompt.project_memory import ProjectMemoryLoader
from paicli.rag import CodeIndex
from paicli.render import PaiCliApp, RichRenderer
from paicli.runtime import DurableTaskManager
from paicli.skill import SkillRegistry
from paicli.snapshot import SnapshotService
from paicli.tools import ToolRegistry

SLASH_COMMANDS = [
    "/help",
    "/exit",
    "/clear",
    "/context",
    "/memory",
    "/save",
    "/config",
    "/tools",
    "/hitl",
    "/policy",
    "/audit",
    "/index",
    "/search",
    "/plan",
    "/team",
    "/model",
    "/skill",
    "/mcp",
    "/browser",
    "/task",
    "/snapshot",
    "/restore",
]

HELP_LINES = [
    "可用命令：",
    "/help - 查看命令帮助",
    "/exit - 退出 PaiCLI",
    "/clear - 清空当前会话历史",
    "/context - 查看当前上下文状态",
    "/memory - 查看记忆系统状态",
    "/memory list - 查看长期记忆列表",
    "/memory search <关键词> - 搜索当前项目可见长期记忆",
    "/memory delete <id> - 删除单条长期记忆",
    "/memory clear - 清空全部长期记忆",
    "/save <事实> - 保存项目级长期记忆",
    "/save --global <事实> - 保存全局长期记忆",
    "/config - 查看当前配置",
    "/tools - 查看可用工具",
    "/model - 查看当前模型",
    "/model <模型名> - 切换当前模型名（重启 REPL 后生效）",
    "/model <provider> <model> - 切换 provider 和模型（重启 REPL 后生效）",
    "/plan - 查看计划模式用法",
    "/plan <任务内容> - 直接用计划模式执行这条任务",
    "/team - 查看 Multi-Agent 模式用法",
    "/team <任务内容> - 直接用多 Agent 协作执行这条任务",
    "/hitl - 查看 HITL 状态",
    "/hitl on - 启用危险操作人工审批",
    "/hitl off - 关闭 HITL 审批",
    "/hitl always|auto|never - 设置 HITL 模式",
    "/policy - 查看安全策略",
    "/audit [N] - 查看最近 N 条审计记录",
    "/browser - 查看浏览器会话状态",
    "/browser connect - 复用已允许远程调试的登录态 Chrome",
    "/browser connect <port> - 旧式 CDP 端口连接",
    "/browser status - 查看浏览器会话状态",
    "/browser tabs - 查看 shared 模式真实 Chrome tab",
    "/browser disconnect - 切回 isolated 浏览器模式",
    "/task - 查看后台任务列表",
    "/task add <任务内容> - 提交后台任务",
    "/task cancel <task_id> - 取消后台任务",
    "/task log <task_id> - 查看后台任务结果",
    "/mcp - 查看 MCP server 状态",
    "/mcp restart <name> - 重启 MCP server",
    "/mcp logs <name> - 查看 MCP server 日志",
    "/mcp disable <name> - 禁用 MCP server",
    "/mcp enable <name> - 启用 MCP server",
    "/mcp resources <name> - 查看 MCP resources",
    "/mcp prompts <name> - 查看 MCP prompts",
    "/skill - 查看可用 Skill",
    "/skill show <name> - 查看指定 Skill 内容",
    "/index [path] - 索引代码库",
    "/search <查询> - 搜索本地代码索引",
    "/snapshot - 查看最近 Side-History 快照",
    "/snapshot status - 查看 Side-Git 快照状态",
    "/snapshot clean - 清理当前项目快照",
    "/restore <snapshot-id-or-index> - 恢复到指定快照",
]


async def start_repl(cwd: str, config: PaiCliConfig) -> None:
    console = Console()
    registry, mcp_manager = await build_tool_registry(config=config, cwd=cwd)
    client = create_llm_client(config.llm)
    system_prompt = PromptAssembler(
        config=config,
        cwd=cwd,
        tool_names=registry.list_names(),
        model=client.model_name,
        provider=client.provider_name,
    ).build()
    agent = Agent(
        llm_client=client,
        tool_registry=registry,
        system_prompt=system_prompt,
        cwd=cwd,
        config=config,
    )

    # Launch Textual TUI app (same pattern as pico: PicoTuiApp(agent).run())
    tui_app = PaiCliApp(
        agent=agent,
        config=config,
        cwd=cwd,
        registry=registry,
        mcp_manager=mcp_manager,
        console=console,
    )
    tui_app._model = client.model_name
    tui_app._context_window = client.max_context_window
    tui_app._provider = client.provider_name

    # Wire the native TUI approval modal as the agent's HITL callback
    agent.approval_callback = tui_app.request_approval

    await tui_app.run_async()



async def _run_agent(agent: Agent, renderer: RichRenderer, message: str) -> None:
    renderer.set_context_window(agent.llm_client.max_context_window)
    renderer.start_run()
    renderer.newline()
    async for event in agent.run(message):
        renderer.handle(event)
        if event.get("type") == "error":
            break
    renderer.newline()


PlanReviewInput = Callable[[ExecutionPlan, bool], Awaitable[PlanReviewDecision]]


async def _run_plan_agent(
    agent: Agent,
    renderer: RichRenderer,
    message: str,
    review_input: PlanReviewInput | None = None,
) -> None:
    # Build project memory for the planner
    project_memory = _extract_project_memory(agent, message)

    async def run_task(
        task: PlanTask,
        completed: dict[str, str],
        *,
        event_sink: Any = None,
    ) -> str:
        """Execute a single plan task with streaming output."""
        context = build_task_context(plan, task)
        task_system = build_task_system_prompt(task)
        prompt = (
            f"Execute plan task `{task.id}`.\n\n"
            f"Task description:\n{task.description}\n\n{context}"
        )

        # Use streaming agent.run() to forward events to renderer
        text = ""
        async for event in agent.run(prompt):
            event_type = event.get("type")
            if event_type == "text_delta":
                text += str(event.get("text") or "")
                if event_sink:
                    event_sink({
                        "type": "task_text_delta",
                        "task_id": task.id,
                        "text": event.get("text"),
                    })
            elif event_type == "thinking_delta":
                if event_sink:
                    event_sink({
                        "type": "task_thinking_delta",
                        "task_id": task.id,
                        "thinking": event.get("thinking"),
                    })
            elif event_type == "tool_call":
                if event_sink:
                    event_sink({
                        "type": "task_tool_call",
                        "task_id": task.id,
                        "name": event.get("name"),
                        "input": event.get("input"),
                    })
            elif event_type == "tool_result":
                if event_sink:
                    event_sink({
                        "type": "task_tool_result",
                        "task_id": task.id,
                        "name": event.get("name"),
                        "result": event.get("result"),
                        "is_error": event.get("is_error"),
                    })
            elif event_type == "error":
                raise event["error"]
        return text

    plan: ExecutionPlan | None = None

    def event_sink(event: dict[str, Any]) -> None:
        renderer.handle(event)

    planner = JsonPlanner(agent.llm_client, project_memory=project_memory)
    executor = PlanExecutor()
    renderer.set_context_window(agent.llm_client.max_context_window)
    renderer.start_run()
    renderer.newline()

    original_goal = message
    planning_goal = message
    while True:
        renderer.handle({"type": "plan_generation_started", "goal": planning_goal})
        plan = await planner.create_plan(planning_goal)
        if planner.last_thinking.strip():
            renderer.handle({"type": "plan_thinking", "thinking": planner.last_thinking})
        renderer.handle({"type": "plan_review_summary", "summary": plan.summary()})
        renderer.handle({"type": "plan_review_instructions"})

        decision = await _review_plan(plan, renderer, review_input)
        if decision.action == "cancel":
            renderer.handle({"type": "plan_cancelled"})
            break
        if decision.action == "supplement":
            planning_goal = f"{original_goal}\n补充要求：{decision.feedback}"
            continue

        # Execute the plan with streaming event forwarding
        failed_tasks: dict[str, str] = {}
        async for event in executor.execute(plan, run_task, event_sink=event_sink):
            renderer.handle(event)
            if event.get("type") == "plan_failed" and event.get("failed"):
                failed_tasks = event["failed"]

        # Replan logic: if failures and progress < 50%, offer to replan
        if failed_tasks and plan and plan.progress_ratio < 0.5:
            completed = {
                t.id: t.result
                for t in plan.tasks
                if t.status == TaskStatus.COMPLETED and t.result
            }
            failure_reason = "; ".join(
                f"{tid}: {err}" for tid, err in failed_tasks.items()
            )
            renderer.handle({
                "type": "plan_replan_prompt",
                "failure_reason": failure_reason,
                "progress": f"{plan.progress_ratio:.0%}",
            })
            replan_plan = await planner.replan(
                original_goal, failure_reason, completed,
            )
            renderer.handle({
                "type": "plan_review_summary",
                "summary": replan_plan.summary(),
            })
            renderer.handle({"type": "plan_review_instructions"})
            replan_decision = await _review_plan(replan_plan, renderer, review_input)
            if replan_decision.action != "cancel":
                plan = replan_plan
                async for event in executor.execute(
                    plan, run_task, event_sink=event_sink,
                ):
                    renderer.handle(event)
            else:
                renderer.handle({"type": "plan_cancelled"})

        break

    renderer.newline()


async def _review_plan(
    plan: ExecutionPlan,
    renderer: RichRenderer,
    review_input: PlanReviewInput | None,
) -> PlanReviewDecision:
    expanded = False
    while True:
        if review_input:
            decision = await review_input(plan, expanded)
        else:
            decision = await _prompt_plan_review_decision(expanded=expanded)

        if decision.action == "expand":
            renderer.handle({"type": "plan_visualization", "visualization": plan.visualize()})
            expanded = True
            continue
        if decision.action == "collapse":
            renderer.handle({"type": "plan_review_summary", "summary": plan.summary()})
            expanded = False
            continue
        if decision.action == "supplement" and not decision.feedback.strip():
            feedback = await _prompt_plan_supplement()
            if feedback.strip():
                return PlanReviewDecision.supplement(feedback.strip())
            renderer.handle({"type": "plan_review_summary", "summary": plan.summary()})
            renderer.handle({"type": "plan_review_instructions"})
            continue
        return decision


async def _prompt_plan_review_decision(*, expanded: bool) -> PlanReviewDecision:
    raw = await _read_plan_review_input()
    return parse_plan_review_input(raw, expanded=expanded)


async def _read_plan_review_input() -> str:
    bindings = KeyBindings()

    @bindings.add("c-o")
    def _expand(event: Any) -> None:
        event.app.exit(result="\x0f")

    @bindings.add("escape")
    def _escape(event: Any) -> None:
        event.app.exit(result="\x1b")

    @bindings.add("i")
    def _supplement(event: Any) -> None:
        buffer = event.app.current_buffer
        if buffer.text:
            buffer.insert_text("i")
            return
        event.app.exit(result="/supplement")

    session = PromptSession(key_bindings=bindings)
    return await session.prompt_async("操作/补充> ")


async def _prompt_plan_supplement() -> str:
    session = PromptSession()
    return await session.prompt_async("补充> ")


async def _handle_slash(
    raw: str,
    console: Console,
    cwd: str,
    config: PaiCliConfig,
    agent: Agent,
    registry: ToolRegistry,
    mcp_manager: McpClientManager | None,
) -> bool:
    command, _, rest = raw.partition(" ")
    arg = rest.strip()
    if command in {"/exit", "/quit"}:
        return True
    if command == "/help":
        console.print(help_text())
    elif command == "/clear":
        agent.clear_history()
        console.clear()
    elif command == "/context":
        memories = MemoryManager(config.memory.long_term_path, project_path=cwd).list(limit=5)
        table = Table(title="PaiCLI Context")
        table.add_column("Field")
        table.add_column("Value")
        table.add_row("cwd", cwd)
        table.add_row("model", f"{config.llm.model} ({config.llm.provider})")
        table.add_row("context window", str(agent.llm_client.max_context_window))
        table.add_row("render", config.render_mode)
        table.add_row("memory", f"{len(memories)} recent entries")
        table.add_row("tools", str(len(registry.list_names())))
        console.print(table)
    elif command == "/memory":
        await _memory_command(arg, console, cwd, config)
    elif command == "/save":
        save_fact, save_scope = _parse_memory_save(arg)
        if not save_fact:
            console.print("[red]Usage:[/red] /save <fact>")
        else:
            memory_id = MemoryManager(config.memory.long_term_path, project_path=cwd).save(
                save_fact,
                scope=save_scope,
            )
            console.print(f"Saved memory {memory_id} ({save_scope})")
    elif command == "/config":
        console.print_json(json.dumps(config_to_public_dict(config), ensure_ascii=False))
    elif command == "/tools":
        console.print("\n".join(registry.list_names()))
    elif command == "/hitl":
        _hitl_command(arg, console, config)
    elif command == "/policy":
        console.print_json(json.dumps(config_to_public_dict(config)["policy"], ensure_ascii=False))
    elif command == "/audit":
        limit = int(arg or "20") if (arg or "20").isdigit() else 20
        console.print_json(
            json.dumps(AuditLog(config.policy.audit_log_path).tail(limit), ensure_ascii=False)
        )
    elif command == "/index":
        count = CodeIndex(cwd).rebuild(arg or ".")
        console.print(f"Indexed {count} code lines.")
    elif command == "/search":
        results = CodeIndex(cwd).search(arg, limit=20)
        output = "\n".join(f"{r.path}:{r.line}: {r.snippet}" for r in results)
        console.print(output or "(no matches)")
    elif command == "/plan":
        if not arg:
            console.print("[red]Usage:[/red] /plan <task>")
        else:
            await _run_plan_agent(
                agent,
                _interactive_renderer(config, provider=agent.llm_client.provider_name),
                arg,
            )
    elif command == "/team":
        if not arg:
            console.print("[red]Usage:[/red] /team <task>")
        else:
            await _run_agent(
                agent,
                _interactive_renderer(config, provider=agent.llm_client.provider_name),
                "Act as planner, worker, and reviewer. "
                "Execute this task and review the result:\n" + arg,
            )
    elif command == "/model":
        _model_command(arg, console, config)
    elif command == "/skill":
        _skill_command(arg, console, cwd)
    elif command == "/mcp":
        await _mcp_command(arg, console, mcp_manager, registry)
    elif command == "/browser":
        _browser_command(arg, console, cwd)
    elif command == "/task":
        _task_command(arg, console)
    elif command == "/snapshot":
        _snapshot_command(arg, console, cwd)
    elif command == "/restore":
        if not arg:
            console.print("[red]Usage:[/red] /restore <snapshot-id-or-index>")
        else:
            record = SnapshotService(cwd).restore(arg)
            console.print(f"Restored {record.id}")
    else:
        console.print(f"[red]Unknown command:[/red] {command}")
    return False


async def _memory_command(arg: str, console: Console, cwd: str, config: PaiCliConfig) -> None:
    manager = MemoryManager(config.memory.long_term_path, project_path=cwd)
    sub, _, rest = arg.partition(" ")
    if sub in {"", "status"}:
        console.print(manager.status())
        console.print(f"Current project: {manager.project_path}")
        console.print("/memory list | /memory search <query> | /memory delete <id> | /memory clear")
    elif sub == "list":
        rows = manager.list(limit=50)
        console.print(_format_memory_entries(rows) or "(no memories)")
    elif sub == "clear":
        count = manager.clear()
        console.print(f"Cleared {count} memories.")
    elif sub == "search":
        rows = manager.search(rest)
        console.print(_format_memory_entries(rows) or "(no matches)")
    elif sub == "delete":
        memory_id = rest.strip()
        if not memory_id:
            console.print("[red]Usage:[/red] /memory delete <id>")
        elif manager.delete(memory_id):
            console.print(f"Deleted memory {memory_id}.")
        else:
            console.print(f"Memory not found: {memory_id}")
    else:
        console.print("[red]Usage:[/red] /memory [list|search <query>|delete <id>|clear]")


def _format_memory_entries(rows: list[Any]) -> str:
    return "\n".join(
        f"{row.id} [{row.scope}] {row.content}"
        for row in rows
    )


def _parse_memory_save(raw: str) -> tuple[str, str]:
    text = raw.strip()
    if not text:
        return "", "project"
    if text.startswith("--global "):
        return text[len("--global ") :].strip(), "global"
    return text, "project"


def _hitl_command(arg: str, console: Console, config: PaiCliConfig) -> None:
    if arg in {"always", "auto", "never"}:
        config.policy.hitl_mode = arg
    elif arg == "on":
        config.policy.hitl_mode = "always"
    elif arg == "off":
        config.policy.hitl_mode = "never"
    console.print(f"HITL mode: {config.policy.hitl_mode}")


def _model_command(arg: str, console: Console, config: PaiCliConfig) -> None:
    if not arg:
        console.print(f"{config.llm.model} ({config.llm.provider})")
        return
    parts = arg.split()
    if len(parts) == 1:
        config.llm.model = parts[0]
    else:
        config.llm.provider = parts[0]
        config.llm.model = parts[1]
    console.print(
        "Model updated for newly created clients. Restart REPL to rebuild the active client."
    )


def _skill_command(arg: str, console: Console, cwd: str) -> None:
    registry = SkillRegistry(cwd)
    sub, _, rest = arg.partition(" ")
    if sub == "show" and rest:
        skill = registry.load(rest.strip())
        if not skill:
            console.print(f'Skill "{rest.strip()}" not found.')
            return
        console.print(skill.content[:12_000])
        return
    rows = registry.list()
    console.print("\n".join(f"{item.name}: {item.description}" for item in rows) or "(no skills)")


def _task_command(arg: str, console: Console) -> None:
    manager = DurableTaskManager(Path.home() / ".paicli" / "tasks" / "tasks.db")
    sub, _, rest = arg.partition(" ")
    if sub == "add" and rest:
        task_id = manager.add(rest)
        console.print(f"Queued {task_id}")
    elif sub == "cancel" and rest:
        console.print(f"Canceled: {manager.cancel(rest.strip())}")
    elif sub == "log" and rest:
        task = manager.get(rest.strip())
        if not task:
            console.print("(task not found)")
        else:
            console.print(task.result or task.error or f"Task {task.id} is {task.status}")
    else:
        rows = manager.list(limit=20)
        console.print(
            "\n".join(f"{task.id} {task.status} {task.prompt[:80]}" for task in rows)
            or "(no tasks)"
        )


def _browser_command(arg: str, console: Console, cwd: str) -> None:
    session = BrowserSession(cwd)
    sub, _, rest = arg.partition(" ")
    try:
        if sub in {"", "status"}:
            state = session.status()
        elif sub == "connect":
            port = int(rest.strip()) if rest.strip() else None
            state = session.connect(port=port)
        elif sub == "disconnect":
            state = session.disconnect()
        elif sub == "tabs":
            tabs = session.tabs()
            if not tabs:
                console.print(
                    "No browser tabs available. Use /browser connect <port> for CDP tabs."
                )
                return
            table = Table(title="Browser Tabs")
            table.add_column("ID")
            table.add_column("Title")
            table.add_column("URL")
            for tab in tabs:
                table.add_row(tab.id, tab.title, tab.url)
            console.print(table)
            return
        else:
            console.print("[red]Usage:[/red] /browser status|connect [port]|disconnect|tabs")
            return
    except ValueError as exc:
        console.print(f"[red]Browser error:[/red] {exc}")
        return
    suffix = f" ({state.browser_url})" if state.browser_url else ""
    console.print(f"Browser mode: {state.mode}{suffix}")


async def _mcp_command(
    arg: str,
    console: Console,
    manager: McpClientManager | None,
    registry: ToolRegistry,
) -> None:
    if manager is None:
        console.print("MCP is disabled.")
        return
    sub, _, rest = arg.partition(" ")
    name = rest.strip()
    if not sub:
        table = Table(title="MCP Servers")
        table.add_column("Name")
        table.add_column("Type")
        table.add_column("Status")
        table.add_column("Target")
        for row in manager.status():
            status = row["status"]
            if row["error"]:
                status = f"{status}: {row['error']}"
            table.add_row(row["name"], row["type"], status, row["target"])
        console.print(table)
        return
    if sub in {"enable", "disable", "restart", "logs", "resources", "prompts"} and not name:
        console.print(f"[red]Usage:[/red] /mcp {sub} <name>")
        return
    if sub == "disable":
        if manager.disable(name):
            removed = registry.unregister_prefix(f"mcp__{name}__")
            console.print(f"Disabled {name}; removed {removed} tools from this session.")
        else:
            console.print(f'MCP server "{name}" not found.')
        return
    if sub == "enable":
        if not manager.enable(name):
            console.print(f'MCP server "{name}" not found.')
            return
        registry.unregister_prefix(f"mcp__{name}__")
        tools = await manager.load_server_tools(name)
        registry.register_all(tools)
        console.print(f"Enabled {name}; loaded {len(tools)} tools.")
        return
    if sub == "restart":
        registry.unregister_prefix(f"mcp__{name}__")
        count = await manager.restart(name)
        tools = await manager.load_server_tools(name)
        registry.register_all(tools)
        console.print(f"Restarted {name}; loaded {len(tools) or count} tools.")
        return
    if sub == "logs":
        console.print(manager.logs(name))
        return
    spec = manager.specs.get(name)
    if not spec:
        console.print(f'MCP server "{name}" not found.')
        return
    if sub == "resources":
        result = await manager.list_resources(spec)
        console.print(result.content)
        return
    if sub == "prompts":
        result = await manager.list_prompts(spec)
        console.print(result.content)
        return
    console.print("[red]Usage:[/red] /mcp [restart|logs|disable|enable|resources|prompts] <name>")


def _snapshot_command(arg: str, console: Console, cwd: str) -> None:
    service = SnapshotService(cwd)
    if arg == "status":
        console.print(service.status())
        return
    if arg == "clean":
        console.print(f"Cleaned {service.clean()} snapshots.")
        return
    rows = service.list(limit=20)
    output = "\n".join(
        f"{index}. {row.id} {row.phase} {row.created_at}" for index, row in enumerate(rows, 1)
    )
    console.print(output or "(no snapshots)")


def _interactive_renderer(
    config: PaiCliConfig,
    *,
    context_window: int | None = None,
    provider: str = "",
) -> RichRenderer:
    renderer = RichRenderer(
        live_markdown=config.render_mode == "inline",
        context_window=context_window,
    )
    renderer.set_provider(provider)
    return renderer


def _approval_prompt(request: dict[str, Any], console: Console, config: PaiCliConfig) -> str:
    if not sys.stdin.isatty():
        return "deny"
    console.print(
        f"[yellow]Approval required[/yellow] {request['tool_name']} "
        f"({request['danger_level']})\n{request['input']}"
    )
    answer = Prompt.ask("Approve?", choices=["y", "n", "a", "s"], default="n")
    if answer == "a":
        config.policy.hitl_mode = "never"
        return "approve"
    if answer == "y":
        return "approve"
    if answer == "s":
        return "skip"
    return "deny"


def _count_mcp_servers(manager: Any) -> int:
    if manager is None:
        return 0
    return sum(1 for spec in manager.specs.values() if spec.enabled)


def _count_named_files(root: str, filename: str) -> int:
    excluded_dirs = {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
    }
    count = 0
    for _dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in excluded_dirs]
        if filename in filenames:
            count += 1
    return count


def _prompt_message(
    *,
    cwd: str,
    model: str,
    tools: int,
    agents_files: int,
    mcp_servers: int,
    skills: int,
    stats: dict[str, Any] | None = None,
) -> list[tuple[str, str]]:
    return [
        ("class:prompt.count.agents", str(agents_files)),
        ("class:prompt.dim", f" {_plural_label(agents_files, 'AGENTS.md file')} · "),
        ("class:prompt.count.mcp", str(mcp_servers)),
        ("class:prompt.dim", f" {_plural_label(mcp_servers, 'MCP server')} · "),
        ("class:prompt.count.skills", str(skills)),
        ("class:prompt.dim", f" {_plural_label(skills, 'skill')} · Tools "),
        ("class:prompt.tools", str(tools)),
        ("class:prompt.dim", "\n"),
        *_bottom_toolbar(cwd, model, stats),
        ("class:prompt.dim", "\n\n"),
        ("class:prompt", "* "),
    ]


def _bottom_toolbar(
    cwd: str,
    model: str,
    stats: dict[str, Any] | None = None,
) -> list[tuple[str, str]]:
    from paicli.render.rich_renderer import format_tokens, format_elapsed, format_cost

    stats = stats or {}
    has_usage = bool(stats.get("has_usage"))
    context_ratio = float(stats.get("context_ratio") or 0)
    context_text = _format_toolbar_percent(context_ratio) if has_usage else "0%"
    context_window = int(stats.get("context_window") or 0)

    segments: list[tuple[str, str]] = [
        # Phase indicator
        ("class:toolbar.phase", f" {_format_phase(stats.get('phase', 'idle'))} "),
        ("class:toolbar.gap", " "),
        # Model name
        ("class:toolbar.model", model),
        ("class:toolbar.gap", "  "),
        # Context bar
        ("class:toolbar.ctx.label", "ctx "),
        ("class:toolbar.ctx.bar", _format_toolbar_bar(context_ratio if has_usage else 0)),
        ("class:toolbar.gap", " "),
        ("class:toolbar.ctx.value", context_text),
    ]

    # Add (used/window) format like (8.7k/1.0M)
    # input_tokens = last API call's prompt_tokens = actual context window usage
    if context_window > 0:
        used_tokens = int(stats.get("input_tokens") or 0)
        segments.append(("class:toolbar.gap", " "))
        segments.append(("class:toolbar.ctx.detail", f"({format_tokens(used_tokens)}/{format_tokens(context_window)})"))

    # Token details (only when we have usage data)
    if has_usage:
        in_tok = int(stats.get("input_tokens") or 0)
        out_tok = int(stats.get("output_tokens") or 0)
        cache_tok = int(stats.get("cached_tokens") or 0)
        segments.append(("class:toolbar.gap", "  "))
        segments.append(("class:toolbar.token.label", "in "))
        segments.append(("class:toolbar.token.value", format_tokens(in_tok)))
        segments.append(("class:toolbar.gap", " "))
        segments.append(("class:toolbar.token.label", "out "))
        segments.append(("class:toolbar.token.value", format_tokens(out_tok)))
        if cache_tok:
            segments.append(("class:toolbar.gap", " "))
            segments.append(("class:toolbar.token.label", "cache "))
            segments.append(("class:toolbar.token.value", format_tokens(cache_tok)))

    # Cost
    cost_val = float(stats.get("cost") or 0)
    cost_str = format_cost(cost_val)
    if cost_str:
        segments.append(("class:toolbar.gap", " \u00b7 "))
        segments.append(("class:toolbar.cost", cost_str))

    # Elapsed
    elapsed_val = float(stats.get("elapsed") or 0)
    if elapsed_val > 0:
        segments.append(("class:toolbar.gap", " "))
        segments.append(("class:toolbar.elapsed", format_elapsed(elapsed_val)))

    # CWD
    segments.append(("class:toolbar.gap", "  "))
    segments.append(("class:toolbar.cwd.value", _shorten_home(cwd)))

    return segments


def _format_phase(phase: str) -> str:
    """Format phase for toolbar display."""
    mapping = {"idle": "IDLE", "running": "RUNNING", "plan": "PLAN"}
    return mapping.get(phase, phase.upper())


def _plural_label(count: int, singular: str) -> str:
    return singular if count == 1 else singular + "s"


def _shorten_home(path: str) -> str:
    home = str(Path.home())
    if path == home:
        return "~"
    prefix = home + os.sep
    if path.startswith(prefix):
        return "~/" + path[len(prefix) :]
    return path


def _format_toolbar_bar(value: float, *, width: int = 12) -> str:
    bounded = max(0.0, min(value, 1.0))
    filled = round(bounded * width)
    if bounded > 0 and filled == 0:
        filled = 1
    return "█" * filled + "░" * (width - filled)


def _format_toolbar_percent(value: float) -> str:
    if value <= 0:
        return "0%"
    if value < 0.01:
        return "<1%"
    return f"{value:.0%}"


def help_text() -> str:
    return "\n".join(HELP_LINES)


def _extract_project_memory(agent: Agent, query: str) -> str:
    """Extract project and relevant long-term memory text for planner context."""
    parts: list[str] = []
    cwd = getattr(agent, "cwd", ".")
    project_memory = ProjectMemoryLoader.create_default(cwd).load_for_prompt()
    if project_memory:
        parts.append(project_memory)
    config = getattr(agent, "config", None)
    if config and config.features.memory and config.memory.long_term_enabled:
        try:
            manager = MemoryManager(config.memory.long_term_path, project_path=cwd)
            long_term = manager.build_context_for_query(query, max_tokens=2000)
            if long_term:
                parts.append(long_term)
        except Exception:
            pass
    return "\n\n".join(parts)[:8000]


def _completed_task_context(completed: dict[str, str]) -> str:
    if not completed:
        return "无已完成的依赖任务。"
    lines = ["已完成的依赖任务结果:"]
    for task_id, result in completed.items():
        preview = result if len(result) <= 4000 else result[:4000] + "\n... [truncated]"
        lines.append(f"\n[{task_id}]\n{preview}")
    return "\n".join(lines)
