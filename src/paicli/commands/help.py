"""Help rendering derived from the command registry."""

from __future__ import annotations

from collections.abc import Iterable

from paicli.commands.registry import CommandRegistry
from paicli.commands.spec import ArgKind, CommandSpec

HELP_NOTES: tuple[str, ...] = (
    "会话与后台任务：",
    (
        "持久化：完整 Session 保存在 ~/.paicli/sessions/*.jsonl，"
        "后台任务状态保存在 ~/.paicli/runtime/tasks.db"
    ),
    "后台任务是提交会话的 child Session",
    "后台任务的对话历史、工具调用状态和待审批状态会持久化",
    "中断的运行中任务不会自动重试，可使用 /task retry",
    "状态：queued | running | waiting_approval | completed | failed | canceled",
)

KEYBOARD_NOTES: tuple[str, ...] = (
    "快捷键：",
    "  Enter       - 发送消息",
    "  Shift+Enter - 换行",
    "  Ctrl+C      - 中断运行中任务 / 空闲时退出",
    "  Ctrl+Q      - 立即退出",
    "  Ctrl+L      - 清屏",
    "  Ctrl+Y      - 切换 HITL / YOLO",
    "  Ctrl+End    - 返回最新消息并恢复跟随",
    "  Up/Down     - 历史浏览",
    "  Tab         - 补全 slash 命令",
    "",
    "内联审批：Y 批准 / N 拒绝 / A 本会话允许 / S 跳过",
    "内联计划：Enter 执行 / Ctrl+O 展开 / I 补充 / Esc 取消",
)


def render_help(registry: CommandRegistry) -> str:
    lines = list(HELP_NOTES[:1])
    session_spec = next(
        (spec for spec in registry.root_specs() if spec.name == "session"),
        None,
    )
    if session_spec is not None:
        lines.extend(_render_specs(session_spec.children, prefix=("session",)))
    lines.extend(HELP_NOTES[1:])
    lines.extend(("", "可用命令："))
    lines.extend(
        _render_specs(
            (spec for spec in registry.root_specs() if spec.name != "session"),
            prefix=(),
        )
    )
    lines.extend(("", *KEYBOARD_NOTES))
    return "\n".join(lines)


def _render_specs(
    specs: Iterable[CommandSpec],
    prefix: tuple[str, ...],
) -> list[str]:
    lines: list[str] = []
    for spec in specs:
        path = (*prefix, spec.name)
        usage = spec.usage if spec.usage is not None else _usage(path, spec)
        if spec.handler is not None and spec.show_in_help:
            lines.append(f"{usage} - {spec.description}")
            lines.extend(
                f"{variant} - {description}"
                for variant, description in spec.help_variants
            )
        lines.extend(_render_specs(spec.children, path))
    return lines


def _usage(path: tuple[str, ...], spec: CommandSpec) -> str:
    parts = [f"/{' '.join(path)}"]
    for argument in spec.args:
        if argument.kind is ArgKind.FLAG:
            part = argument.option_names()[0]
        elif argument.kind is ArgKind.REST:
            part = f"<{argument.name}>"
        else:
            part = f"<{argument.name}>"
        if not argument.required:
            part = f"[{part}]"
        parts.append(part)
    return " ".join(parts)
