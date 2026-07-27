from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def adapter_module(monkeypatch):
    class FakeInstalledAgent:
        def __init__(self, *args, logs_dir, **kwargs):
            del args, kwargs
            self.logs_dir = Path(logs_dir)
            self.root_commands: list[str] = []
            self.agent_commands: list[tuple[str, dict | None]] = []

        async def exec_as_root(self, _environment, *, command):
            self.root_commands.append(command)

        async def exec_as_agent(self, _environment, *, command, env=None):
            self.agent_commands.append((command, env))

    def with_prompt_template(function):
        return function

    modules = {
        "harbor": types.ModuleType("harbor"),
        "harbor.agents": types.ModuleType("harbor.agents"),
        "harbor.agents.installed": types.ModuleType("harbor.agents.installed"),
        "harbor.agents.installed.base": types.ModuleType("harbor.agents.installed.base"),
        "harbor.environments": types.ModuleType("harbor.environments"),
        "harbor.environments.base": types.ModuleType("harbor.environments.base"),
        "harbor.models": types.ModuleType("harbor.models"),
        "harbor.models.agent": types.ModuleType("harbor.models.agent"),
        "harbor.models.agent.context": types.ModuleType("harbor.models.agent.context"),
    }
    modules["harbor.agents.installed.base"].BaseInstalledAgent = FakeInstalledAgent
    modules["harbor.agents.installed.base"].with_prompt_template = with_prompt_template
    modules["harbor.environments.base"].BaseEnvironment = object
    modules["harbor.models.agent.context"].AgentContext = object
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    sys.modules.pop("benchmark.harbor.paicli_agent", None)
    return importlib.import_module("benchmark.harbor.paicli_agent")


class FakeEnvironment:
    default_user = "agent"
    session_id = "trial/id"

    def __init__(self):
        self.uploads: list[tuple[Path, str, list[str]]] = []

    async def upload_dir(self, source, destination):
        root = Path(source)
        files = sorted(
            path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
        )
        self.uploads.append((root, destination, files))


def _source_tree(root: Path) -> Path:
    package = root / "src" / "paicli"
    package.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "[project]\nname='paicli-python'\nversion='1.0'\n", encoding="utf-8"
    )
    (root / "README.md").write_text("# PaiCLI\n", encoding="utf-8")
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / ".env").write_text("PAICLI_API_KEY=not-uploaded\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_extra.py").write_text("pass\n", encoding="utf-8")
    return root


def test_harbor_agent_installs_minimal_checkout_and_runs_one_benchmark_turn(
    tmp_path: Path,
    adapter_module,
) -> None:
    source = _source_tree(tmp_path / "source")
    environment = FakeEnvironment()
    agent = adapter_module.PaiCliHarborAgent(
        logs_dir=tmp_path / "logs",
        source_dir=source,
        max_turns="80",
        max_tool_calls="160",
        max_elapsed_seconds="1200",
        max_total_tokens="600000",
    )

    asyncio.run(agent.install(environment))
    asyncio.run(agent.run("Fix the task.\nRun tests.", environment, object()))

    source_upload = next(upload for upload in environment.uploads if upload[1].endswith("src"))
    assert source_upload[2] == [
        ".paicli-source-identity.json",
        "README.md",
        "pyproject.toml",
        "src/paicli/__init__.py",
    ]
    identity = json.loads(
        (source_upload[0] / ".paicli-source-identity.json").read_text(encoding="utf-8")
    )
    assert identity["content_sha256"]
    assert identity["dirty"] is False
    assert not any(".env" in file for file in source_upload[2])
    assert not any(file.startswith("tests/") for file in source_upload[2])

    command, command_env = agent.agent_commands[-1]
    assert "exec /opt/paicli-agent/.venv/bin/python -m paicli" in command
    assert "--benchmark --cwd ." in command
    assert "--max-turns 80" in command
    assert "--max-tool-calls 160" in command
    assert "--max-elapsed-seconds 1200.0" in command
    assert "--max-total-tokens 600000" in command
    assert "'Fix the task." in command
    assert "PAICLI_API_KEY" not in command
    assert command_env == {"PAICLI_BENCHMARK": "1"}


def test_harbor_agent_uses_only_an_explicit_package_fallback(
    tmp_path: Path,
    adapter_module,
) -> None:
    environment = FakeEnvironment()
    package = "https://example.invalid/paicli.zip"
    agent = adapter_module.PaiCliHarborAgent(
        logs_dir=tmp_path / "logs",
        source_dir=tmp_path / "missing",
        package=package,
    )

    asyncio.run(agent.install(environment))

    install_command, install_env = agent.agent_commands[0]
    assert package in install_command
    assert install_env is None
    assert not any(
        destination.endswith("src") for _root, destination, _files in environment.uploads
    )


def test_harbor_agent_bootstraps_python_and_venv_support(
    tmp_path: Path,
    adapter_module,
) -> None:
    agent = adapter_module.PaiCliHarborAgent(logs_dir=tmp_path)

    command = agent._python_setup_command()

    assert 'MISSING_PACKAGES="python3 python3-venv"' in command
    assert "python3.11 python3.11-venv" in command
    assert 'VENV_PROBE="$(mktemp -d)"' in command
    assert "python3-venv" in command
    assert "apt-get -o Acquire::Retries=3 update" in command
    assert "attempting install from available signed indexes" in command
    assert "apt-get -o Acquire::Retries=3 install -y --no-install-recommends" in command


def test_harbor_agent_stages_an_explicit_mcp_configuration(
    tmp_path: Path,
    adapter_module,
) -> None:
    source = _source_tree(tmp_path / "source")
    mcp_config = tmp_path / "mcp.json"
    mcp_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "echo": {
                        "command": "echo-server",
                        "env": {"TOKEN": "${MCP_TOKEN}"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    environment = FakeEnvironment()
    agent = adapter_module.PaiCliHarborAgent(
        logs_dir=tmp_path / "logs",
        source_dir=source,
        mcp_config_path=mcp_config,
    )

    asyncio.run(agent.install(environment))
    asyncio.run(agent.run("Use the project tools.", environment, object()))

    assert any(destination.endswith("mcp") for _root, destination, _files in environment.uploads)
    command, _env = agent.agent_commands[-1]
    assert "--benchmark-mcp-config /installed-agent/paicli-mcp/mcp.json" in command


def test_harbor_agent_rejects_literal_mcp_credentials(tmp_path: Path, adapter_module) -> None:
    mcp_config = tmp_path / "mcp.json"
    mcp_config.write_text(
        json.dumps({"mcpServers": {"echo": {"env": {"TOKEN": "literal-secret"}}}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="environment references"):
        adapter_module.PaiCliHarborAgent(
            logs_dir=tmp_path / "logs",
            source_dir=tmp_path,
            mcp_config_path=mcp_config,
        )


def test_harbor_agent_allows_literal_non_secret_mcp_settings(
    tmp_path: Path,
    adapter_module,
) -> None:
    mcp_config = tmp_path / "mcp.json"
    mcp_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "echo": {
                        "env": {"LOG_LEVEL": "info"},
                        "headers": {"Accept": "application/json"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    agent = adapter_module.PaiCliHarborAgent(
        logs_dir=tmp_path / "logs",
        source_dir=tmp_path,
        mcp_config_path=mcp_config,
    )

    assert agent is not None


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("max_turns", 0),
        ("max_tool_calls", 0),
        ("max_elapsed_seconds", 0),
        ("max_total_tokens", 0),
    ],
)
def test_harbor_agent_rejects_invalid_resource_limits(
    tmp_path: Path,
    adapter_module,
    argument: str,
    value: int,
) -> None:
    with pytest.raises(ValueError, match=argument):
        adapter_module.PaiCliHarborAgent(logs_dir=tmp_path, **{argument: value})
