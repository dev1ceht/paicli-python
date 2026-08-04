"""Public interfaces for the shared slash-command seam."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, TypeAlias


class ArgKind(StrEnum):
    WORD = "word"
    REST = "rest"
    INT = "int"
    CHOICE = "choice"
    PATH = "path"
    FLAG = "flag"


class BusyPolicy(StrEnum):
    ALLOW = "allow"
    REJECT = "reject"


class ErrorPolicy(StrEnum):
    USER = "user"
    PROPAGATE = "propagate"


@dataclass(frozen=True, slots=True)
class ArgumentSpec:
    """One positional argument or flag accepted by a command."""

    name: str
    kind: ArgKind = ArgKind.WORD
    description: str = ""
    required: bool = False
    default: Any = None
    choices: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    completion: CompletionProvider | None = None

    @property
    def is_flag(self) -> bool:
        return self.kind is ArgKind.FLAG

    def option_names(self) -> tuple[str, ...]:
        """Return accepted long-option spellings for a flag argument."""
        if not self.is_flag:
            return ()
        generated = "--" + self.name.replace("_", "-")
        names = [generated]
        for alias in self.aliases:
            names.append(alias if alias.startswith("-") else "--" + alias)
        return tuple(dict.fromkeys(names))


@dataclass(frozen=True, slots=True)
class CompletionItem:
    label: str
    value: str | None = None
    description: str = ""
    kind: str = "command"
    score: float = 0.0

    @property
    def insert_text(self) -> str:
        return self.value if self.value is not None else self.label


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    text: str
    cursor: int


@dataclass(frozen=True, slots=True)
class CommandMessage:
    level: str
    text: str


@dataclass(frozen=True, slots=True)
class CommandEffect:
    """Marker base class for UI effects returned by a command handler."""


@dataclass(frozen=True, slots=True)
class ExitApp(CommandEffect):
    pass


@dataclass(frozen=True, slots=True)
class ClearScreen(CommandEffect):
    pass


@dataclass(frozen=True, slots=True)
class RefreshHitlBanner(CommandEffect):
    pass


@dataclass(frozen=True, slots=True)
class RefreshStatus(CommandEffect):
    pass


@dataclass(frozen=True, slots=True)
class RefreshContextUsage(CommandEffect):
    pass


@dataclass(frozen=True, slots=True)
class ModelChanged(CommandEffect):
    model: str
    provider: str
    context_window: int


@dataclass(frozen=True, slots=True)
class CommandResult:
    messages: tuple[CommandMessage, ...] = ()
    effects: tuple[CommandEffect, ...] = ()

    @classmethod
    def empty(cls) -> CommandResult:
        return cls()


class CommandHost(Protocol):
    """Temporary adapter for behavior that is still owned by an entrypoint.

    The host is deliberately narrower than a Textual App. It is a migration seam:
    command handlers can stop calling it as domain handlers move into this package.
    """

    def dispatch_registered_command(
        self,
        invocation: ParsedCommand,
    ) -> CommandResult | Awaitable[CommandResult] | None: ...


@dataclass(slots=True)
class CommandContext:
    cwd: Path
    host: CommandHost | None = None
    services: Any = None
    registry: Any = None
    config: Any = None
    agent: Any = None
    session: Any = None
    tool_registry: Any = None
    mcp_manager: Any = None
    agent_running: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    raw: str
    path: tuple[str, ...]
    values: Mapping[str, Any]
    tokens: tuple[str, ...]
    spec: CommandSpec


def legacy_argument_text(invocation: ParsedCommand) -> str:
    """Serialize parsed values for a legacy handler during migration.

    The legacy adapter receives canonical values from ``CommandSpec`` rather
    than reparsing the user's raw string. This helper can be removed once all
    handlers accept ``ParsedCommand`` directly.
    """
    parts = list(invocation.path[1:])
    for argument in invocation.spec.args:
        value = invocation.values.get(argument.name)
        if argument.is_flag:
            if value:
                parts.append(argument.option_names()[0])
        elif value is not None:
            parts.append(str(value))
    return " ".join(parts)


CommandHandler: TypeAlias = Callable[
    [CommandContext, ParsedCommand],
    CommandResult | Awaitable[CommandResult] | None,
]
CompletionProvider: TypeAlias = Callable[
    [CompletionRequest, CommandContext],
    Sequence[CompletionItem] | Awaitable[Sequence[CompletionItem]],
]
AvailabilityCheck: TypeAlias = Callable[[CommandContext], str | None]


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """Declarative command metadata and its executable handler.

    ``name`` and aliases never include the leading slash. Nested commands are
    represented by ``children`` so the same tree drives parsing, help, and
    completion.
    """

    name: str
    description: str
    handler: CommandHandler | None = None
    args: tuple[ArgumentSpec, ...] = ()
    children: tuple[CommandSpec, ...] = ()
    aliases: tuple[str, ...] = ()
    usage: str | None = None
    help_variants: tuple[tuple[str, str], ...] = ()
    completion: CompletionProvider | None = None
    availability: AvailabilityCheck | None = None
    show_in_help: bool = True
    busy_policy: BusyPolicy = BusyPolicy.ALLOW
    error_policy: ErrorPolicy = ErrorPolicy.USER


class CommandError(Exception):
    """An expected, user-facing command failure."""

    def __init__(self, message: str, *, usage: str | None = None, code: str = "command_error"):
        super().__init__(message)
        self.user_message = message
        self.usage = usage
        self.code = code


class CommandUnavailable(CommandError):
    """A command is valid but cannot run in the current application state."""
