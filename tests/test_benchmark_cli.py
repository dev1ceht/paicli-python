from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from typer.testing import CliRunner

from paicli.entrypoints.cli import app
from paicli.tools import get_builtin_tools


@pytest.fixture
def model_endpoint():
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP handler API
            length = int(self.headers.get("content-length", "0"))
            requests.append(json.loads(self.rfile.read(length)))
            if len(requests) == 1:
                arguments = json.dumps(
                    {
                        "path": "implemented.txt",
                        "content": "created by benchmark\n",
                    }
                )
                chunks = [
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call_write",
                                            "function": {
                                                "name": "write_file",
                                                "arguments": arguments,
                                            },
                                        }
                                    ]
                                },
                                "finish_reason": "tool_calls",
                            }
                        ]
                    }
                ]
            else:
                chunks = [
                    {
                        "choices": [
                            {
                                "delta": {"content": "Harbor task complete. benchmark-secret"},
                                "finish_reason": None,
                            }
                        ]
                    },
                    {
                        "choices": [],
                        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                    },
                    {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                ]
            body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            body += "data: [DONE]\n\n"
            encoded = body.encode()
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_benchmark_cli_runs_one_production_turn_with_isolated_configuration(
    tmp_path: Path,
    model_endpoint,
) -> None:
    endpoint, requests = model_endpoint
    project = tmp_path / "project"
    config_dir = project / ".paicli"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "llm": {"model": "project-model", "temperature": 1.9},
                "agent": {"max_turns": 1},
            }
        ),
        encoding="utf-8",
    )
    (config_dir / "mcp.json").write_text(
        json.dumps({"mcpServers": {"project-server": {"command": "missing-project-command"}}}),
        encoding="utf-8",
    )
    (project / ".env").write_text("PAICLI_MODEL=dotenv-model\n", encoding="utf-8")
    log_dir = tmp_path / "logs"
    home = tmp_path / "clean-home"

    result = CliRunner().invoke(
        app,
        [
            "--benchmark",
            "--cwd",
            str(project),
            "--prompt",
            "Create the requested file.",
            "--benchmark-log-dir",
            str(log_dir),
            "--max-turns",
            "100",
            "--max-tool-calls",
            "200",
            "--max-elapsed-seconds",
            "1800",
            "--max-total-tokens",
            "1000000",
        ],
        env={
            "HOME": str(home),
            "PAICLI_PROVIDER": "openai-compatible",
            "PAICLI_MODEL": "harbor-model",
            "PAICLI_BASE_URL": endpoint,
            "PAICLI_API_KEY": "benchmark-secret",
        },
    )

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "Harbor task complete. [REDACTED]"
    assert (project / "implemented.txt").read_text(encoding="utf-8") == ("created by benchmark\n")
    assert len(requests) == 2
    assert requests[0]["model"] == "harbor-model"
    assert requests[0]["temperature"] == 0.7
    assert sorted(tool["function"]["name"] for tool in requests[0]["tools"]) == sorted(
        tool.name for tool in get_builtin_tools()
    )

    runtime = json.loads((log_dir / "runtime.json").read_text(encoding="utf-8"))
    assert runtime["status"] == "completed"
    timestamp_pattern = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"
    assert re.fullmatch(timestamp_pattern, runtime["started_at"])
    assert re.fullmatch(timestamp_pattern, runtime["completed_at"])
    assert runtime["provider"] == "openai-compatible"
    assert runtime["model"] == "harbor-model"
    assert runtime["agent_budget"] == {
        "max_turns": 100,
        "max_tool_calls": 200,
        "max_elapsed_seconds": 1800.0,
        "max_total_tokens": 1_000_000,
    }
    assert runtime["tools"] == sorted(tool.name for tool in get_builtin_tools())
    assert runtime["mcp_servers"] == []
    assert "benchmark-secret" not in (log_dir / "events.jsonl").read_text(encoding="utf-8")
    assert (log_dir / "final.txt").read_text(encoding="utf-8") == (
        "Harbor task complete. [REDACTED]\n"
    )
    assert "completed" in (log_dir / "paicli.txt").read_text(encoding="utf-8")


def test_benchmark_cli_requires_a_single_non_interactive_prompt(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["--benchmark", "--cwd", str(tmp_path)],
        env={"PAICLI_API_KEY": "benchmark-secret"},
    )

    assert result.exit_code == 2
    assert "--benchmark requires --prompt" in result.output


def test_benchmark_cli_rejects_zero_resource_limits(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "--benchmark",
            "--cwd",
            str(tmp_path),
            "--prompt",
            "test",
            "--max-turns",
            "0",
        ],
        env={"PAICLI_API_KEY": "benchmark-secret"},
    )

    assert result.exit_code == 2
    assert "benchmark resource limits must be positive" in result.output
