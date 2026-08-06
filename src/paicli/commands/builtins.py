"""Built-in command catalog.

Core lifecycle and inspection commands are implemented here. More stateful
commands temporarily delegate to the entrypoint host while they migrate one
vertical slice at a time.
"""

from __future__ import annotations

import inspect
import json

from paicli.commands.help import render_help
from paicli.commands.spec import (
    ArgKind,
    ArgumentSpec,
    AvailabilityCheck,
    BusyPolicy,
    ClearScreen,
    CommandContext,
    CommandError,
    CommandHandler,
    CommandMessage,
    CommandResult,
    CommandSpec,
    CompletionProvider,
    ErrorPolicy,
    ExitApp,
    ModelChanged,
    ParsedCommand,
    RefreshContextUsage,
    RefreshHitlBanner,
    RefreshStatus,
)


def _delegate(context: CommandContext, invocation: ParsedCommand):
    if context.host is None:
        raise RuntimeError("command host is not configured")
    result = context.host.dispatch_registered_command(invocation)
    if result is None:
        return CommandResult.empty()
    if inspect.isawaitable(result):
        return _await_delegate(result)
    if isinstance(result, CommandResult):
        return result
    raise RuntimeError("synchronous command host returned an awaitable")


async def _await_delegate(result) -> CommandResult:
    resolved = await result
    if resolved is None:
        return CommandResult.empty()
    if isinstance(resolved, CommandResult):
        return resolved
    raise RuntimeError("command host returned an unsupported result")


def _handle_help(context: CommandContext, _invocation: ParsedCommand) -> CommandResult:
    if context.registry is None:
        raise RuntimeError("command registry is not configured")
    return CommandResult(messages=(CommandMessage("info", render_help(context.registry)),))


def _handle_exit(_context: CommandContext, _invocation: ParsedCommand) -> CommandResult:
    return CommandResult(effects=(ExitApp(),))


def _handle_clear(_context: CommandContext, _invocation: ParsedCommand) -> CommandResult:
    return CommandResult(effects=(ClearScreen(),))


def _handle_reset(context: CommandContext, _invocation: ParsedCommand) -> CommandResult:
    if context.session is not None:
        context.session.reset_context()
    if context.agent is not None:
        context.agent.clear_history()
    return CommandResult(
        effects=(ClearScreen(), RefreshContextUsage(), RefreshStatus()),
    )


def _handle_tools(context: CommandContext, _invocation: ParsedCommand) -> CommandResult:
    if context.tool_registry is None:
        return CommandResult(messages=(CommandMessage("info", "(no tools)"),))
    names = context.tool_registry.list_names()
    return CommandResult(messages=(CommandMessage("info", "\n".join(names)),))


def _handle_config(context: CommandContext, _invocation: ParsedCommand) -> CommandResult:
    from paicli.config import config_to_public_dict

    if context.config is None:
        raise RuntimeError("config is not configured")
    payload = json.dumps(config_to_public_dict(context.config), ensure_ascii=False, indent=2)
    return CommandResult(messages=(CommandMessage("info", payload),))


def _handle_policy(context: CommandContext, _invocation: ParsedCommand) -> CommandResult:
    from paicli.config import config_to_public_dict

    if context.config is None:
        raise RuntimeError("config is not configured")
    payload = json.dumps(
        config_to_public_dict(context.config)["policy"],
        ensure_ascii=False,
        indent=2,
    )
    return CommandResult(messages=(CommandMessage("info", payload),))


def _handle_audit(context: CommandContext, invocation: ParsedCommand) -> CommandResult:
    from paicli.policy import AuditLog

    if context.config is None:
        raise RuntimeError("config is not configured")
    limit = int(invocation.values.get("N") or 20)
    payload = json.dumps(
        AuditLog(context.config.policy.audit_log_path).tail(limit),
        ensure_ascii=False,
        indent=2,
    )
    return CommandResult(messages=(CommandMessage("info", payload),))


def _handle_hitl(context: CommandContext, invocation: ParsedCommand) -> CommandResult:
    if context.config is None:
        raise RuntimeError("config is not configured")

    requested = invocation.values.get("mode")
    if requested is not None:
        context.config.policy.hitl_mode = {
            "on": "always",
            "off": "never",
        }.get(str(requested), str(requested))
    return CommandResult(
        messages=(CommandMessage("info", f"HITL mode: {context.config.policy.hitl_mode}"),),
        effects=(RefreshHitlBanner(),),
    )


def _handle_thinking(context: CommandContext, invocation: ParsedCommand) -> CommandResult:
    if context.config is None or context.agent is None:
        raise RuntimeError("Agent is not initialized")

    from paicli.llm.capabilities import available_thinking_levels, resolve_thinking_level
    from paicli.thinking import THINKING_LEVEL_SET

    model = context.agent.llm_client.model_name
    requested = invocation.values.get("level")
    if requested is None:
        current = context.config.llm.thinking_level or "auto"
        available = ", ".join(available_thinking_levels(model))
        return CommandResult(
            messages=(
                CommandMessage(
                    "info",
                    f"thinking: {current}\navailable: {available}",
                ),
            )
        )

    if requested == "auto":
        normalized = resolve_thinking_level(model, None)
        persisted = None
    elif requested in THINKING_LEVEL_SET:
        normalized = resolve_thinking_level(model, str(requested))
        persisted = normalized
    else:
        raise ValueError("invalid thinking level")

    context.config.llm.thinking_level = normalized
    context.agent.llm_client.thinking_level = normalized
    if context.session is not None:
        context.session.record_thinking_level(persisted)
    return CommandResult(
        messages=(CommandMessage("info", f"thinking: {normalized or 'auto'}"),),
        effects=(RefreshStatus(),),
    )


def _handle_model(context: CommandContext, invocation: ParsedCommand) -> CommandResult:
    if context.config is None or context.agent is None:
        raise RuntimeError("Agent is not initialized")

    first = invocation.values.get("provider-or-model")
    second = invocation.values.get("model")
    if not first:
        return CommandResult(
            messages=(
                CommandMessage(
                    "info",
                    f"{context.config.llm.model} ({context.config.llm.provider})",
                ),
            )
        )

    from paicli.config import load_llm_config_for_provider

    if second:
        provider, model = str(first), str(second)
    else:
        provider, model = context.config.llm.provider, str(first)
    llm_config = load_llm_config_for_provider(context.cwd, provider, model)
    llm_config.thinking_level = context.config.llm.thinking_level
    llm_config.thinking_budget = context.config.llm.thinking_budget
    if not llm_config.api_key:
        raise CommandError(f"Model switch failed: no API key configured for {provider}.")

    client = context.agent.reconfigure_llm(llm_config)
    model_name = str(client.model_name)
    provider_name = str(client.provider_name)
    context_window = int(client.max_context_window)
    return CommandResult(
        messages=(
            CommandMessage(
                "success",
                f"Model switched to {model_name} ({provider_name}).",
            ),
        ),
        effects=(
            ModelChanged(
                model=model_name,
                provider=provider_name,
                context_window=context_window,
            ),
        ),
    )


def _word(name: str, description: str = "", *, required: bool = False) -> ArgumentSpec:
    return ArgumentSpec(
        name=name,
        kind=ArgKind.WORD,
        description=description,
        required=required,
    )


def _rest(name: str, description: str = "", *, required: bool = False) -> ArgumentSpec:
    return ArgumentSpec(
        name=name,
        kind=ArgKind.REST,
        description=description,
        required=required,
    )


def _choice(
    name: str,
    choices: tuple[str, ...],
    description: str = "",
) -> ArgumentSpec:
    return ArgumentSpec(
        name=name,
        kind=ArgKind.CHOICE,
        choices=choices,
        description=description,
    )


def _flag(name: str, description: str = "") -> ArgumentSpec:
    return ArgumentSpec(name=name, kind=ArgKind.FLAG, description=description)


def _command(
    name: str,
    description: str,
    *,
    args: tuple[ArgumentSpec, ...] = (),
    children: tuple[CommandSpec, ...] = (),
    aliases: tuple[str, ...] = (),
    usage: str | None = None,
    help_variants: tuple[tuple[str, str], ...] = (),
    handler: CommandHandler = _delegate,
    completion: CompletionProvider | None = None,
    availability: AvailabilityCheck | None = None,
    show_in_help: bool = True,
    busy_policy: BusyPolicy = BusyPolicy.ALLOW,
    error_policy: ErrorPolicy = ErrorPolicy.USER,
) -> CommandSpec:
    return CommandSpec(
        name=name,
        description=description,
        handler=handler,
        args=args,
        children=children,
        aliases=aliases,
        usage=usage,
        help_variants=help_variants,
        show_in_help=show_in_help,
        busy_policy=busy_policy,
        completion=completion,
        availability=availability,
        error_policy=error_policy,
    )


def build_builtin_specs() -> tuple[CommandSpec, ...]:
    session_children = (
        _command("list", "查看当前工作区的会话", usage="/session list"),
        _command(
            "show",
            "查看会话详情与 catalog 摘要",
            args=(_word("session-id"),),
            usage="/session show [session-id]",
        ),
        _command("stats", "查看当前会话的持久化用量与活动统计", usage="/session stats"),
        _command(
            "rename",
            "重命名当前会话",
            args=(_rest("title", required=True),),
            usage="/session rename <title>",
        ),
        _command(
            "new",
            "创建并切换到新会话",
            args=(_rest("title"),),
            usage="/session new [title]",
        ),
        _command(
            "resume",
            "恢复指定会话",
            args=(_word("session-id"),),
            usage="/session resume <session-id>",
            help_variants=(("/session resume", "打开交互式会话选择器"),),
        ),
        _command(
            "share",
            "导出脱敏 Markdown",
            args=(_word("session-id"), _flag("include_tool_results")),
            usage="/session share [session-id] [--include-tool-results]",
        ),
        _command(
            "fork",
            "从当前边界派生新会话",
            args=(_rest("title"),),
            usage="/session fork [title]",
        ),
        _command("archive", "归档当前会话", usage="/session archive"),
        _command("delete", "将当前会话移入回收站", usage="/session delete"),
        _command(
            "restore",
            "从回收站恢复会话",
            args=(_word("session-id", required=True),),
            usage="/session restore <session-id>",
        ),
    )

    memory_children = (
        _command("list", "查看长期记忆列表", usage="/memory list"),
        _command(
            "search",
            "搜索当前项目可见长期记忆",
            args=(_rest("关键词", required=True),),
            usage="/memory search <关键词>",
        ),
        _command(
            "delete",
            "删除单条长期记忆",
            args=(_word("id", required=True),),
            usage="/memory delete <id>",
        ),
        _command("clear", "清空全部长期记忆", usage="/memory clear"),
        _command("pending", "查看待确认的记忆变更", usage="/memory pending"),
        _command(
            "apply",
            "确认待处理记忆变更",
            args=(_word("id", required=True),),
            usage="/memory apply <id>",
        ),
        _command(
            "reject",
            "拒绝待处理记忆变更",
            args=(_word("id", required=True),),
            usage="/memory reject <id>",
        ),
    )

    mcp_children = tuple(
        _command(
            name,
            description,
            args=(_word("name", required=True),),
            usage=f"/mcp {name} <name>",
        )
        for name, description in (
            ("restart", "重启 MCP server"),
            ("logs", "查看 MCP server 日志"),
            ("disable", "禁用 MCP server"),
            ("enable", "启用 MCP server"),
            ("resources", "查看 MCP resources"),
            ("prompts", "查看 MCP prompts"),
        )
    )

    return (
        _command(
            "session",
            "管理持久化会话",
            children=session_children,
            show_in_help=False,
        ),
        _command("help", "查看命令帮助", usage="/help", handler=_handle_help),
        _command(
            "exit",
            "退出 PaiCLI",
            aliases=("quit",),
            usage="/exit",
            handler=_handle_exit,
        ),
        _command(
            "clear",
            "清空屏幕显示（保留 Session history 和模型上下文）",
            usage="/clear",
            handler=_handle_clear,
        ),
        _command(
            "reset",
            "清空屏幕与后续模型上下文（保留 Session history）",
            usage="/reset",
            handler=_handle_reset,
        ),
        _command("context", "查看当前上下文状态", usage="/context"),
        _command(
            "compact",
            "立即摘要较早的模型上下文并保留最近回合",
            args=(_rest("instruction"),),
            usage="/compact [instruction]",
            busy_policy=BusyPolicy.REJECT,
        ),
        _command("memory", "查看记忆系统状态", children=memory_children),
        _command(
            "save",
            "保存项目级长期记忆",
            args=(_flag("global"), _rest("事实", required=True)),
            usage="/save [--global] <事实>",
            help_variants=(("/save --global <事实>", "保存全局长期记忆"),),
        ),
        _command("config", "查看当前配置", usage="/config", handler=_handle_config),
        _command(
            "thinking",
            "查看或切换当前推理等级",
            args=(
                _choice(
                    "level",
                    ("off", "on", "minimal", "low", "medium", "high", "xhigh", "max", "auto"),
                ),
            ),
            usage="/thinking [level|auto]",
            handler=_handle_thinking,
        ),
        _command("tools", "查看可用工具", usage="/tools", handler=_handle_tools),
        _command(
            "model",
            "查看当前模型",
            args=(_word("provider-or-model"), _word("model")),
            usage="/model",
            help_variants=(
                ("/model <模型名>", "热切换当前 provider 的模型（空闲时立即生效）"),
                (
                    "/model <provider> <model>",
                    "热切换 provider 和模型，并读取该 provider 的 .env 凭据",
                ),
            ),
            busy_policy=BusyPolicy.REJECT,
            handler=_handle_model,
        ),
        _command(
            "plan",
            "查看计划模式用法",
            args=(_rest("任务内容"),),
            usage="/plan",
            help_variants=(("/plan <任务内容>", "直接用计划模式执行这条任务"),),
        ),
        _command(
            "hitl",
            "查看 HITL 状态",
            args=(_choice("mode", ("on", "off", "always", "auto", "never")),),
            usage="/hitl",
            help_variants=(
                ("/hitl on", "启用危险操作人工审批"),
                ("/hitl off", "关闭 HITL 审批"),
                ("/hitl always|auto|never", "设置 HITL 模式"),
            ),
            handler=_handle_hitl,
        ),
        _command("policy", "查看安全策略", usage="/policy", handler=_handle_policy),
        _command(
            "audit",
            "查看最近 N 条审计记录",
            args=(ArgumentSpec("N", kind=ArgKind.INT),),
            usage="/audit [N]",
            handler=_handle_audit,
        ),
        _command(
            "index",
            "索引代码库",
            args=(_word("path"),),
            usage="/index [path]",
        ),
        _command(
            "search",
            "搜索本地代码索引",
            args=(_rest("查询", required=True),),
            usage="/search <查询>",
        ),
        _command(
            "mcp",
            "查看 MCP server 状态",
            children=mcp_children,
        ),
        _command(
            "browser",
            "查看浏览器会话状态",
            children=tuple(
                _command(
                    name,
                    description,
                    usage=usage,
                    args=args,
                    help_variants=(
                        (("/browser connect <port>", "旧式 CDP 端口连接"),)
                        if name == "connect"
                        else ()
                    ),
                )
                for name, description, usage, args in (
                    (
                        "connect",
                        "复用已允许远程调试的登录态 Chrome",
                        "/browser connect",
                        (_word("port"),),
                    ),
                    ("status", "查看浏览器会话状态", "/browser status", ()),
                    ("tabs", "查看 shared 模式真实 Chrome tab", "/browser tabs", ()),
                    ("disconnect", "切回 isolated 浏览器模式", "/browser disconnect", ()),
                )
            ),
        ),
        _command(
            "task",
            "查看后台任务列表",
            children=tuple(
                _command(
                    name,
                    description,
                    args=(_rest("value", required=True),),
                    usage=(
                        f"/task {name} <task_id|N|latest>"
                        if name != "add"
                        else "/task add <任务内容>"
                    ),
                )
                for name, description in (
                    ("add", "提交后台任务"),
                    ("approve", "批准等待中的后台任务操作"),
                    ("deny", "拒绝等待中的后台任务操作"),
                    ("cancel", "取消后台任务"),
                    ("retry", "重试失败的后台任务"),
                    ("log", "查看后台任务结果"),
                )
            ),
        ),
        _command(
            "skill",
            "查看可用 Skill",
            children=(
                _command(
                    "show",
                    "查看指定 Skill 内容",
                    args=(_word("name", required=True),),
                    usage="/skill show <name>",
                ),
            ),
        ),
        _command(
            "checkpoint",
            "管理独立的工作区 checkpoint",
            children=(
                _command(
                    "create",
                    "创建工作区 checkpoint",
                    args=(_rest("label"),),
                    usage="/checkpoint create [label]",
                ),
                _command("list", "查看工作区 checkpoint", usage="/checkpoint list"),
                _command("status", "查看 checkpoint 状态", usage="/checkpoint status"),
                _command(
                    "restore",
                    "恢复到指定 checkpoint",
                    args=(_word("checkpoint-id-or-index", required=True),),
                    usage="/checkpoint restore <checkpoint-id-or-index>",
                ),
                _command("clean", "清理工作区 checkpoint", usage="/checkpoint clean"),
            ),
        ),
    )
