from __future__ import annotations

import json
import re
from pathlib import Path

from paicli.session.models import SessionMessage
from paicli.session.repository import SessionRepository

_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:ghp|github_pat|xox[baprs])_[A-Za-z0-9_-]{8,}\b"),
)
_NAMED_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|token|password|secret)"
    r"(\s*[:=]\s*)[^\s,;]+"
)
_WINDOWS_PATH = re.compile(r"(?i)\b[A-Z]:\\[^\s`\"']+")
_POSIX_PRIVATE_PATH = re.compile(
    r"(?<![\w:])/(?:home|Users|root|tmp|var|etc|opt|srv)(?:/[^\s`\"']*)?"
)


class SessionShareService:
    """Render a durable session as a safely redacted Markdown transcript."""

    def __init__(
        self,
        repository: SessionRepository,
        *,
        output_dir: str | Path | None = None,
    ) -> None:
        self.repository = repository
        self.output_dir = (
            Path(output_dir).expanduser()
            if output_dir is not None
            else repository.db_path.parent / "shares"
        )

    def export_markdown(
        self,
        session_id: str,
        *,
        output_path: str | Path | None = None,
        include_tool_results: bool = False,
    ) -> Path:
        record = self.repository.get_session(session_id)
        if record is None:
            raise KeyError(f"session not found: {session_id}")
        view = self.repository.rebuild_session_view(session_id)
        target = (
            Path(output_path).expanduser()
            if output_path is not None
            else self.output_dir / f"{session_id}.md"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        markdown = self._render(
            record=record,
            messages=view.session_history,
            include_tool_results=include_tool_results,
        )
        temporary = target.with_suffix(f"{target.suffix}.tmp")
        temporary.write_text(markdown, encoding="utf-8")
        temporary.replace(target)
        return target.resolve()

    def _render(
        self,
        *,
        record,
        messages: tuple[SessionMessage, ...],
        include_tool_results: bool,
    ) -> str:
        model = f"{record.provider}/{record.model}" if record.model else "unknown"
        lines = [
            f"# {redact_text(record.title)}",
            "",
            f"- Session: `{record.id}`",
            f"- Workspace: {redact_text(record.workspace_root)}",
            f"- Created: {record.created_at}",
            f"- Updated: {record.updated_at}",
            f"- Model: {redact_text(model)}",
            "",
            "> Sensitive tokens and local absolute paths are redacted by default.",
            "",
        ]
        omitted_tool_results = False
        for message in messages:
            if message.hidden:
                continue
            if message.role == "tool":
                if not include_tool_results:
                    omitted_tool_results = True
                    continue
                lines.extend(["### Tool result", "", redact_text(message.content), ""])
                continue
            heading = "User" if message.role == "user" else "Assistant"
            if message.status == "partial":
                heading += " (interrupted)"
            lines.extend([f"## {heading}", ""])
            for part in message.parts:
                if part.kind == "text" and part.content:
                    lines.extend([redact_text(part.content), ""])
                elif part.kind == "tool_call":
                    tool_name = str(part.metadata.get("tool_name") or "unknown")
                    arguments = json.dumps(
                        part.metadata.get("arguments") or {},
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    lines.extend(
                        [
                            f"### Tool call: `{redact_text(tool_name)}`",
                            "",
                            "```json",
                            redact_text(arguments),
                            "```",
                            "",
                        ]
                    )
        if omitted_tool_results:
            lines.extend(
                [
                    "> Tool results were omitted. Re-export with "
                    "`--include-tool-results` to include redacted results.",
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"


def redact_text(value: str) -> str:
    result = str(value)
    result = _NAMED_SECRET.sub(r"\1\2[REDACTED_SECRET]", result)
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("[REDACTED_SECRET]", result)
    result = _WINDOWS_PATH.sub("[REDACTED_PATH]", result)
    return _POSIX_PRIVATE_PATH.sub("[REDACTED_PATH]", result)
