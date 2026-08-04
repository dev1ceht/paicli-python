"""Command tree, lookup, help traversal, and slash completion."""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from dataclasses import dataclass

from paicli.commands.parser import bind_arguments, tokenize
from paicli.commands.spec import (
    CommandContext,
    CommandError,
    CommandSpec,
    CompletionItem,
    CompletionProvider,
    CompletionRequest,
    ParsedCommand,
)


@dataclass(frozen=True, slots=True)
class _CompletionResolution:
    node: CommandSpec
    parts: tuple[str, ...]
    trailing_space: bool
    consumed_children: int
    current_token_is_argument: bool
    partial_child_prefix: str | None


class CommandRegistry:
    """Immutable command catalog used by every interactive entrypoint."""

    def __init__(self, specs: Iterable[CommandSpec]):
        self._specs = tuple(specs)
        self._validate_group(self._specs, prefix=())
        self._roots = self._index(self._specs)

    @classmethod
    def from_groups(cls, *groups: Iterable[CommandSpec]) -> CommandRegistry:
        return cls(spec for group in groups for spec in group)

    @staticmethod
    def _index(specs: Iterable[CommandSpec]) -> dict[str, CommandSpec]:
        return {
            name.removeprefix("/").lower(): spec
            for spec in specs
            for name in (spec.name, *spec.aliases)
        }

    @classmethod
    def _validate_group(cls, specs: Iterable[CommandSpec], prefix: tuple[str, ...]) -> None:
        names: dict[str, str] = {}
        for spec in specs:
            if not spec.name or spec.name.startswith("/") or " " in spec.name:
                raise ValueError(f"invalid command name at {prefix}: {spec.name!r}")
            for name in (spec.name, *spec.aliases):
                key = name.removeprefix("/").lower()
                previous = names.get(key)
                if previous is not None:
                    path = " ".join((*prefix, name))
                    raise ValueError(f"duplicate command name or alias at /{path}: {name}")
                names[key] = name
            if spec.handler is None and not spec.children:
                raise ValueError(f"command /{' '.join((*prefix, spec.name))} has no handler")
            cls._validate_group(spec.children, (*prefix, spec.name))

    def root_specs(self) -> tuple[CommandSpec, ...]:
        return self._specs

    def root_command_names(self) -> list[str]:
        return [f"/{spec.name}" for spec in self._specs]

    def iter_specs(self) -> Iterable[tuple[tuple[str, ...], CommandSpec]]:
        yield from self._iter_specs(self._specs, ())

    @classmethod
    def _iter_specs(
        cls,
        specs: Iterable[CommandSpec],
        prefix: tuple[str, ...],
    ) -> Iterable[tuple[tuple[str, ...], CommandSpec]]:
        for spec in specs:
            path = (*prefix, spec.name)
            yield path, spec
            yield from cls._iter_specs(spec.children, path)

    def parse(self, raw: str) -> ParsedCommand:
        tokens = tokenize(raw)
        if not tokens or not tokens[0].startswith("/"):
            raise CommandError("不是 slash 命令", code="not_command")

        root_name = tokens[0].removeprefix("/").lower()
        spec = self._roots.get(root_name)
        if spec is None:
            raise CommandError(f"未知命令：/{root_name}", code="unknown_command")

        path: list[str] = [spec.name]

        argument_tokens = tokens[1:]
        while argument_tokens and spec.children:
            candidate = argument_tokens[0].lower()
            child = self._index(spec.children).get(candidate)
            if child is None:
                break
            path.append(child.name)
            spec = child
            argument_tokens = argument_tokens[1:]

        if spec.handler is None:
            raise CommandError(
                f"命令不完整：/{' '.join(path)}",
                code="incomplete_command",
            )

        values = bind_arguments(spec, argument_tokens)
        return ParsedCommand(
            raw=raw,
            path=tuple(path),
            values=values,
            tokens=tuple(argument_tokens),
            spec=spec,
        )

    def complete_sync(
        self,
        request: CompletionRequest,
        context: CommandContext,
    ) -> list[CompletionItem]:
        """Return static or synchronous dynamic completions for a draft."""
        text = request.text[: request.cursor]
        if not text.startswith("/"):
            return []

        parts = tuple(text.split())
        if len(parts) == 1 and not text.endswith((" ", "\t")):
            return self._command_items(self._specs, parts[0][1:].lower(), leading_slash=True)

        resolution = self._resolve_completion(text)
        if resolution is None:
            return []

        child_items = self._child_completion_items(resolution)
        if child_items is not None:
            return child_items

        flag_items = self._flag_completion_items(resolution)
        if flag_items is not None:
            return flag_items

        static_items = self._argument_static_items(resolution)
        if static_items:
            return static_items

        provider = self._completion_provider(resolution)
        if provider is not None:
            result = provider(request, context)
            if inspect.isawaitable(result):
                self._close_awaitable(result)
            else:
                return list(result)
        return []

    async def complete(
        self,
        request: CompletionRequest,
        context: CommandContext,
    ) -> list[CompletionItem]:
        text = request.text[: request.cursor]
        if not text.startswith("/"):
            return []

        parts = tuple(text.split())
        if len(parts) == 1 and not text.endswith((" ", "\t")):
            return self._command_items(self._specs, parts[0][1:].lower(), leading_slash=True)

        resolution = self._resolve_completion(text)
        if resolution is None:
            return []

        child_items = self._child_completion_items(resolution)
        if child_items is not None:
            return child_items

        flag_items = self._flag_completion_items(resolution)
        if flag_items is not None:
            return flag_items

        static_items = self._argument_static_items(resolution)
        if static_items:
            return static_items

        provider = self._completion_provider(resolution)
        if provider is None:
            return []
        result = provider(request, context)
        if inspect.isawaitable(result):
            result = await result
        return list(result)

    def _resolve_completion(self, text: str) -> _CompletionResolution | None:
        trailing_space = text.endswith((" ", "\t"))
        parts = tuple(text.split())
        if not parts:
            return None

        node = self._roots.get(parts[0][1:].lower())
        if node is None:
            return None

        consumed_children = 0
        current_token_is_argument = False
        partial_child_prefix: str | None = None
        for token_index, token in enumerate(parts[1:], start=1):
            is_current = token_index == len(parts) - 1 and not trailing_space
            child = self._index(node.children).get(token.lower())
            if child is None:
                if is_current:
                    partial_child_prefix = token
                current_token_is_argument = True
                break
            node = child
            consumed_children += 1

        return _CompletionResolution(
            node=node,
            parts=parts,
            trailing_space=trailing_space,
            consumed_children=consumed_children,
            current_token_is_argument=current_token_is_argument,
            partial_child_prefix=partial_child_prefix,
        )

    def _child_completion_items(
        self,
        resolution: _CompletionResolution,
    ) -> list[CompletionItem] | None:
        if resolution.partial_child_prefix is not None:
            items = self._command_items(
                resolution.node.children,
                resolution.partial_child_prefix,
                leading_slash=False,
            )
            if items:
                return items
        if (
            not resolution.current_token_is_argument
            and resolution.trailing_space
            and resolution.node.children
        ):
            return self._command_items(resolution.node.children, "", leading_slash=False)
        return None

    def _argument_static_items(
        self,
        resolution: _CompletionResolution,
    ) -> list[CompletionItem]:
        argument_index = max(
            0,
            len(resolution.parts) - 1 - resolution.consumed_children - 1,
        )
        argument_specs = [argument for argument in resolution.node.args if not argument.is_flag]
        if argument_index >= len(argument_specs):
            return []

        argument = argument_specs[argument_index]
        if not argument.choices:
            return []
        prefix = "" if resolution.trailing_space else resolution.parts[-1].lower()
        return [
            CompletionItem(choice, kind="argument")
            for choice in argument.choices
            if choice.lower().startswith(prefix)
        ]

    def _flag_completion_items(
        self,
        resolution: _CompletionResolution,
    ) -> list[CompletionItem] | None:
        flags = [argument for argument in resolution.node.args if argument.is_flag]
        if not flags:
            return None

        current = resolution.parts[-1] if not resolution.trailing_space else ""
        if current and not current.startswith("--"):
            return None

        provided = {
            token.lower()
            for token in resolution.parts
            if token.startswith("--")
        }
        prefix = current.lower()
        return [
            CompletionItem(
                argument.option_names()[0],
                description=argument.description,
                kind="option",
            )
            for argument in flags
            if argument.option_names()[0].lower() not in provided
            and argument.option_names()[0].lower().startswith(prefix)
        ]

    def _completion_provider(
        self,
        resolution: _CompletionResolution,
    ) -> CompletionProvider | None:
        if (
            not resolution.current_token_is_argument
            and resolution.trailing_space
            and resolution.node.children
        ):
            return None
        argument_index = max(
            0,
            len(resolution.parts) - 1 - resolution.consumed_children - 1,
        )
        argument_specs = [argument for argument in resolution.node.args if not argument.is_flag]
        if argument_index < len(argument_specs):
            provider = argument_specs[argument_index].completion
            if provider is not None:
                return provider
        return resolution.node.completion

    @staticmethod
    def _close_awaitable(value: object) -> None:
        close = getattr(value, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _command_items(
        specs: Iterable[CommandSpec],
        prefix: str,
        *,
        leading_slash: bool,
    ) -> list[CompletionItem]:
        lowered = prefix.lower()
        items: list[CompletionItem] = []
        for spec in specs:
            if spec.name.lower().startswith(lowered):
                items.append(
                    CompletionItem(
                        label=f"{'/' if leading_slash else ''}{spec.name}",
                        description=spec.description,
                        kind="command",
                    )
                )
        return items
