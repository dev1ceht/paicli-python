"""Shared slash-command models, registry, parsing, and execution."""

from functools import lru_cache

from paicli.commands.builtins import build_builtin_specs
from paicli.commands.executor import CommandExecutor
from paicli.commands.help import render_help
from paicli.commands.registry import CommandRegistry
from paicli.commands.spec import (
    ArgKind,
    ArgumentSpec,
    AvailabilityCheck,
    BusyPolicy,
    ClearScreen,
    CommandContext,
    CommandEffect,
    CommandError,
    CommandHandler,
    CommandMessage,
    CommandResult,
    CommandSpec,
    CommandUnavailable,
    CompletionItem,
    CompletionProvider,
    CompletionRequest,
    ErrorPolicy,
    ExitApp,
    ModelChanged,
    ParsedCommand,
    RefreshContextUsage,
    RefreshHitlBanner,
    RefreshStatus,
    legacy_argument_text,
)


@lru_cache(maxsize=1)
def default_registry() -> CommandRegistry:
    """Return the process-wide immutable built-in command catalog."""
    return CommandRegistry(build_builtin_specs())


__all__ = [
    "ArgKind",
    "ArgumentSpec",
    "AvailabilityCheck",
    "BusyPolicy",
    "ClearScreen",
    "CommandContext",
    "CommandEffect",
    "CommandError",
    "CommandExecutor",
    "CommandMessage",
    "CommandRegistry",
    "CommandResult",
    "CommandSpec",
    "CommandHandler",
    "CommandUnavailable",
    "CompletionItem",
    "CompletionProvider",
    "CompletionRequest",
    "ErrorPolicy",
    "ExitApp",
    "ModelChanged",
    "ParsedCommand",
    "RefreshContextUsage",
    "RefreshHitlBanner",
    "RefreshStatus",
    "default_registry",
    "legacy_argument_text",
    "render_help",
]
