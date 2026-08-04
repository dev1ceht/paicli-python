"""Tokenization and typed argument binding for slash commands."""

from __future__ import annotations

from collections.abc import Iterable

from paicli.commands.spec import ArgKind, ArgumentSpec, CommandError, CommandSpec


def tokenize(raw: str) -> list[str]:
    """Tokenize a slash command without treating Windows separators as escapes."""
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    token_started = False
    index = 0
    text = raw.strip()

    while index < len(text):
        char = text[index]
        if quote is not None:
            if char == quote:
                quote = None
                token_started = True
                index += 1
                continue
            if quote == '"' and char == "\\":
                next_char = text[index + 1] if index + 1 < len(text) else ""
                if next_char in {'"', "\\"}:
                    current.append(next_char)
                    token_started = True
                    index += 2
                    continue
            current.append(char)
            token_started = True
            index += 1
            continue

        if char in {"'", '"'}:
            quote = char
            token_started = True
            index += 1
            continue

        if char == "\\":
            next_char = text[index + 1] if index + 1 < len(text) else ""
            if next_char in {"\\", '"', "'", " ", "\t"}:
                current.append(next_char)
                token_started = True
                index += 2
                continue
            current.append(char)
            token_started = True
            index += 1
            continue

        if char.isspace():
            if token_started:
                tokens.append("".join(current))
                current.clear()
                token_started = False
            index += 1
            continue

        current.append(char)
        token_started = True
        index += 1

    if quote is not None:
        raise CommandError("命令语法错误：未闭合的引号", code="invalid_syntax")
    if token_started:
        tokens.append("".join(current))
    return tokens


def bind_arguments(spec: CommandSpec, tokens: Iterable[str]) -> dict[str, object]:
    """Bind positional tokens and flags according to one CommandSpec."""
    remaining = list(tokens)
    values: dict[str, object] = {
        argument.name: argument.default
        for argument in spec.args
        if argument.default is not None
    }
    values.update(
        {argument.name: False for argument in spec.args if argument.is_flag}
    )

    flags = {
        option: argument
        for argument in spec.args
        if argument.is_flag
        for option in argument.option_names()
    }
    positional: list[str] = []
    index = 0
    while index < len(remaining):
        token = remaining[index]
        argument = flags.get(token)
        if argument is not None:
            values[argument.name] = True
        elif token.startswith("--"):
            raise CommandError(f"未知选项：{token}", usage=spec.usage, code="unknown_option")
        else:
            positional.append(token)
        index += 1

    positional_specs = [argument for argument in spec.args if not argument.is_flag]
    position_index = 0
    for argument in positional_specs:
        if argument.kind is ArgKind.REST:
            raw_value = " ".join(positional[position_index:]).strip()
            if raw_value:
                values[argument.name] = raw_value
            position_index = len(positional)
            break

        if position_index >= len(positional):
            if argument.required:
                raise CommandError(
                    f"缺少参数：{argument.name}",
                    usage=spec.usage,
                    code="missing_argument",
                )
            continue

        raw_value = positional[position_index]
        values[argument.name] = _convert(argument, raw_value, spec.usage)
        position_index += 1

    if position_index < len(positional):
        raise CommandError(
            f"参数过多：{positional[position_index]}",
            usage=spec.usage,
            code="too_many_arguments",
        )

    return values


def _convert(argument: ArgumentSpec, raw_value: str, usage: str | None) -> object:
    if argument.kind is ArgKind.INT:
        try:
            return int(raw_value)
        except ValueError as exc:
            raise CommandError(
                f"参数 {argument.name} 必须是整数：{raw_value}",
                usage=usage,
                code="invalid_argument",
            ) from exc

    if argument.kind is ArgKind.CHOICE and raw_value not in argument.choices:
        choices = "、".join(argument.choices)
        raise CommandError(
            f"参数 {argument.name} 必须是：{choices}",
            usage=usage,
            code="invalid_argument",
        )

    return raw_value
