from __future__ import annotations

import asyncio
from typing import Any

from paicli.cancellation import TaskCanceled
from paicli.policy import AuditLog
from paicli.policy.command_guard import CommandGuard
from paicli.tools.base import Tool, ToolContext, ToolDecision, ToolResult
from paicli.tools.registry import ToolRegistry


class ToolExecutor:
    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    async def execute_all(
        self,
        calls: list[dict[str, Any]],
        context: ToolContext,
    ) -> list[ToolResult]:
        context.raise_if_cancelled()
        read_calls: list[tuple[dict[str, Any], Tool]] = []
        sequential_calls: list[tuple[dict[str, Any], Tool | None]] = []

        for call in calls:
            name = _tool_call_name(call)
            tool = self.registry.get(name)
            if tool and tool.is_read_only and tool.is_concurrency_safe:
                read_calls.append((call, tool))
            else:
                sequential_calls.append((call, tool))

        results: list[ToolResult] = []
        if read_calls:
            semaphore = asyncio.Semaphore(context.config.tools.max_concurrent_read)

            async def run_read(call: dict[str, Any], tool: Tool) -> ToolResult:
                async with semaphore:
                    context.raise_if_cancelled()
                    return await self._execute_single(call, tool, context)

            results.extend(
                await asyncio.gather(*(run_read(call, tool) for call, tool in read_calls))
            )

        for call, tool in sequential_calls:
            context.raise_if_cancelled()
            results.append(await self._execute_single(call, tool, context))

        return results

    async def _execute_single(
        self,
        call: dict[str, Any],
        tool: Tool | None,
        context: ToolContext,
    ) -> ToolResult:
        context.raise_if_cancelled()
        tool_call_id = str(call.get("id") or "")
        name = _tool_call_name(call)
        payload = _tool_call_arguments(call)

        if not tool:
            return ToolResult(
                tool_use_id=tool_call_id,
                content=(
                    f'Tool "{name}" not found. Available tools: '
                    f"{', '.join(self.registry.list_names())}"
                ),
                is_error=True,
            )

        audit = AuditLog(context.config.policy.audit_log_path)
        approver = "none"
        try:
            data = tool.validate(payload)
            self._preflight(tool, data, context)
            if _must_audit(tool):
                audit.ensure_available()
            decision = await self._approval_decision(tool, data, context)
            decision_source = "prompt"
            if decision == "allow_session":
                context.session_allowed_tools.add(tool.name)
                decision = "approve"
                decision_source = "session_allowlist"
            if decision in {"deny", "skip"}:
                approver = "hitl"
                audit.record(
                    tool_name=tool.name,
                    input_data=data,
                    outcome=decision,
                    approver=approver,
                    cwd=context.cwd,
                    decision_source=decision_source,
                )
                return ToolResult(
                    tool_use_id=tool_call_id,
                    content=f'Tool "{tool.name}" was {decision}ed by approval policy.',
                    is_error=True,
                )
            if tool.requires_approval or context.config.policy.hitl_mode == "always":
                approver = "hitl"

            result = await tool.execute(data, context)
            result.tool_use_id = tool_call_id
            if _must_audit(tool):
                audit.record(
                    tool_name=tool.name,
                    input_data=data,
                    outcome="allow" if not result.is_error else "error",
                    approver=approver,
                    cwd=context.cwd,
                    result_summary=result.display_summary or result.content[:2000],
                    decision_source=("unattended" if context.config.policy.hitl_mode == "never" else decision_source),
                )
            return result
        except TaskCanceled:
            raise
        except Exception as exc:  # noqa: BLE001 - tool errors must flow back to the model
            if tool and _must_audit(tool):
                try:
                    audit.record(tool_name=tool.name, input_data=payload, outcome="error", approver=approver, cwd=context.cwd, reason=str(exc))
                except OSError:
                    pass
            return ToolResult(
                tool_use_id=tool_call_id,
                content=f'Tool "{name}" execution error: {exc}',
                is_error=True,
            )

    async def _approval_decision(
        self,
        tool: Tool,
        payload: dict[str, Any],
        context: ToolContext,
    ) -> ToolDecision:
        mode = context.config.policy.hitl_mode
        if tool.mandatory_confirmation:
            pass
        elif mode == "never":
            return "approve"
        elif tool.name in context.session_allowed_tools:
            return "approve"
        if (
            mode == "auto"
            and not tool.requires_approval
            and not (context.config.policy.require_approval_for_writes and not tool.is_read_only)
        ):
            return "approve"
        if not context.approval_callback:
            return "deny"
        result = context.approval_callback(
            {
                "tool_name": tool.name,
                "input": payload,
                "danger_level": tool.danger_level,
                "description": tool.description,
            }
        )
        if asyncio.iscoroutine(result):
            result = await result
        return result

    def _preflight(self, tool: Tool, payload: dict[str, Any], context: ToolContext) -> None:
        if tool.name in {"bash", "execute_command"}:
            CommandGuard(context.config.policy.command_blacklist).validate(str(payload["command"]))


def _tool_call_name(call: dict[str, Any]) -> str:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    return str(function.get("name") or call.get("name") or "")


def _tool_call_arguments(call: dict[str, Any]) -> dict[str, Any]:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    arguments = function.get("arguments", call.get("arguments", {}))
    if isinstance(arguments, str):
        import json

        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            parsed = {"raw": arguments}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    return arguments if isinstance(arguments, dict) else {}


def _must_audit(tool: Tool) -> bool:
    return tool.name.startswith("mcp__") or not tool.is_read_only
