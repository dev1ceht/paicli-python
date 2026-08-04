"""Command validation and execution policy."""

from __future__ import annotations

import asyncio
import inspect
import logging

from paicli.commands.registry import CommandRegistry
from paicli.commands.spec import (
    BusyPolicy,
    CommandContext,
    CommandError,
    CommandMessage,
    CommandResult,
    ErrorPolicy,
)

logger = logging.getLogger(__name__)


class CommandExecutor:
    """Small execution interface hiding parsing, guards, and error policy."""

    def __init__(self, registry: CommandRegistry):
        self.registry = registry

    async def execute(self, raw: str, context: CommandContext) -> CommandResult:
        parsed = None
        try:
            parsed = self.registry.parse(raw)
            self._check_state(parsed.spec, context)
            result = parsed.spec.handler(context, parsed)  # type: ignore[misc]
            if inspect.isawaitable(result):
                result = await result
            return result if isinstance(result, CommandResult) else CommandResult.empty()
        except asyncio.CancelledError:
            raise
        except CommandError as exc:
            return self._error_result(exc)
        except Exception:
            error_policy = parsed.spec.error_policy if parsed is not None else ErrorPolicy.USER
            logger.exception("slash command failed: %s", raw)
            if error_policy is ErrorPolicy.PROPAGATE:
                raise
            return CommandResult(
                messages=(CommandMessage("error", "命令执行失败，详细信息已写入日志。"),)
            )

    def execute_sync(self, raw: str, context: CommandContext) -> CommandResult:
        """Execute handlers that complete synchronously.

        Entrypoints such as Textual can keep their current synchronous event
        handlers while individual commands schedule their own workers. A handler
        returning an awaitable is rejected rather than silently leaked.
        """
        parsed = None
        try:
            parsed = self.registry.parse(raw)
            self._check_state(parsed.spec, context)
            result = parsed.spec.handler(context, parsed)  # type: ignore[misc]
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise CommandError(
                    "该命令需要异步执行，请稍后重试。",
                    code="async_required",
                )
            return result if isinstance(result, CommandResult) else CommandResult.empty()
        except CommandError as exc:
            return self._error_result(exc)
        except Exception:
            logger.exception("synchronous slash command failed: %s", raw)
            error_policy = parsed.spec.error_policy if parsed is not None else ErrorPolicy.USER
            if error_policy is ErrorPolicy.PROPAGATE:
                raise
            return CommandResult(
                messages=(CommandMessage("error", "命令执行失败，详细信息已写入日志。"),)
            )

    @staticmethod
    def _check_state(spec, context: CommandContext) -> None:
        if context.agent_running and spec.busy_policy is BusyPolicy.REJECT:
            raise CommandError("Agent 正在运行，当前命令暂不可用。", code="agent_busy")
        if spec.availability is not None:
            reason = spec.availability(context)
            if reason:
                raise CommandError(reason, usage=spec.usage, code="unavailable")

    @staticmethod
    def _error_result(error: CommandError) -> CommandResult:
        message = error.user_message
        if error.usage:
            message = f"{message}\nUsage: {error.usage}"
        return CommandResult(messages=(CommandMessage("error", message),))
