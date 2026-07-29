from __future__ import annotations

from paicli.session import (
    SessionRepository,
    SessionShareService,
    ToolActionSpec,
)


def test_markdown_share_redacts_sensitive_content_and_omits_tool_results(tmp_path):
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path, title="Share me")
    repository.append_message(
        session.id,
        role="user",
        content=(
            r'Read D:\private\plan.md using api_key=sk-super-secret-value '
            'and password: "two word password"'
        ),
    )
    repository.append_message(
        session.id,
        role="assistant",
        content="I inspected the file.",
    )
    repository.append_message(
        session.id,
        role="tool",
        content="tool output contains sk-tool-secret and /home/alice/private.txt",
    )
    service = SessionShareService(repository)

    output_path = service.export_markdown(
        session.id,
        output_path=tmp_path / "shared.md",
    )
    markdown = output_path.read_text(encoding="utf-8")

    assert "# Share me" in markdown
    assert "I inspected the file." in markdown
    assert "[REDACTED_PATH]" in markdown
    assert "[REDACTED_SECRET]" in markdown
    assert "sk-super-secret-value" not in markdown
    assert "two word password" not in markdown
    assert "tool output contains" not in markdown
    assert "Tool results were omitted" in markdown


def test_markdown_share_can_include_redacted_tool_results(tmp_path):
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path, title="Tool share")
    repository.append_message(
        session.id,
        role="tool",
        content="result from /home/alice/file.txt",
    )

    path = SessionShareService(repository).export_markdown(
        session.id,
        output_path=tmp_path / "with-tools.md",
        include_tool_results=True,
    )
    markdown = path.read_text(encoding="utf-8")

    assert "### Tool result" in markdown
    assert "result from [REDACTED_PATH]" in markdown
    assert "/home/alice/file.txt" not in markdown


def test_markdown_share_redacts_json_tool_arguments(tmp_path):
    repository = SessionRepository(tmp_path / "sessions.db")
    session = repository.create_session(tmp_path, title="JSON secrets")
    repository.begin_turn(session.id, turn_id="turn_secret", user_content="use tool")
    repository.prepare_tool_actions(
        session.id,
        turn_id="turn_secret",
        model_turn=1,
        assistant_content="",
        actions=(
            ToolActionSpec(
                tool_call_id="call_secret",
                tool_name="login",
                arguments={"password": "two word password", "username": "alice"},
                raw_call={},
                is_read_only=False,
                is_idempotent=False,
            ),
        ),
    )

    path = SessionShareService(repository).export_markdown(
        session.id,
        output_path=tmp_path / "json.md",
    )
    markdown = path.read_text(encoding="utf-8")

    assert "two word password" not in markdown
    assert '"password": "[REDACTED_SECRET]"' in markdown
    assert '"username": "alice"' in markdown
