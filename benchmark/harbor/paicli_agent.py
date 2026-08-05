"""Harbor installed-agent adapter for PaiCLI."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Final

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

_AGENT_ROOT: Final = "/opt/paicli-agent"
_REMOTE_SOURCE_DIR: Final = "/installed-agent/paicli-src"
_REMOTE_MCP_DIR: Final = "/installed-agent/paicli-mcp"
_REMOTE_METADATA_DIR: Final = "/installed-agent/paicli-metadata"
_BENCHMARK_HOME: Final = "/tmp/paicli-harbor-home"
_LOG_DIR: Final = "/logs/agent"
_ENV_REFERENCE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
_CREDENTIAL_MARKERS: Final = (
    "API_KEY",
    "AUTHORIZATION",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET",
    "TOKEN",
)


class PaiCliHarborAgent(BaseInstalledAgent):
    """Install PaiCLI in a Harbor task container and run one production turn."""

    def __init__(
        self,
        *args,
        source_dir: str | Path | None = None,
        package: str | None = None,
        mcp_config_path: str | Path | None = None,
        max_turns: int | str = 100,
        max_tool_calls: int | str = 200,
        max_elapsed_seconds: float | str = 1800,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._source_dir = (
            Path(source_dir).expanduser().resolve()
            if source_dir is not None
            else _default_source_dir()
        )
        self._package = package
        self._mcp_config_path = (
            Path(mcp_config_path).expanduser().resolve() if mcp_config_path is not None else None
        )
        self._max_turns = _positive_int(max_turns, "max_turns")
        self._max_tool_calls = _positive_int(max_tool_calls, "max_tool_calls")
        self._max_elapsed_seconds = _positive_float(max_elapsed_seconds, "max_elapsed_seconds")
        self._remote_identity_path = f"{_REMOTE_SOURCE_DIR}/.paicli-source-identity.json"
        if self._mcp_config_path is not None:
            _validate_mcp_config(self._mcp_config_path)

    @staticmethod
    def name() -> str:
        return "paicli"

    def get_version_command(self) -> str | None:
        return (
            f"{shlex.quote(_venv_python())} -c "
            "\"from importlib.metadata import version; print(version('paicli-python'))\""
        )

    async def install(self, environment: BaseEnvironment) -> None:
        install_spec = await self._prepare_install_spec(environment)
        await self._prepare_mcp_config(environment)
        agent_user = str(environment.default_user or "root")
        quoted_user = shlex.quote(agent_user)
        await self.exec_as_root(
            environment,
            command=(
                "set -eu; "
                f"mkdir -p {shlex.quote(_AGENT_ROOT)}; "
                f"chown -R {quoted_user}:{quoted_user} {shlex.quote(_AGENT_ROOT)}"
            ),
        )
        await self.exec_as_root(environment, command=self._python_setup_command())
        await self.exec_as_agent(environment, command=_install_command(install_spec))

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        del context
        await self.exec_as_agent(
            environment,
            command=self._run_command(instruction),
            env={"PAICLI_BENCHMARK": "1"},
        )

    async def _prepare_install_spec(self, environment: BaseEnvironment) -> str:
        if self._package is not None:
            metadata = self._stage_package_identity()
            await self.exec_as_root(
                environment,
                command=(
                    "set -eu; "
                    f"rm -rf {shlex.quote(_REMOTE_METADATA_DIR)}; "
                    f"mkdir -p {shlex.quote(_REMOTE_METADATA_DIR)}"
                ),
            )
            await environment.upload_dir(metadata, _REMOTE_METADATA_DIR)
            self._remote_identity_path = f"{_REMOTE_METADATA_DIR}/.paicli-source-identity.json"
            return self._package

        staged = self._stage_local_source()
        await self.exec_as_root(
            environment,
            command=(
                "set -eu; "
                f"rm -rf {shlex.quote(_REMOTE_SOURCE_DIR)}; "
                f"mkdir -p {shlex.quote(_REMOTE_SOURCE_DIR)}"
            ),
        )
        await environment.upload_dir(staged, _REMOTE_SOURCE_DIR)
        agent_user = str(environment.default_user or "root")
        await self.exec_as_root(
            environment,
            command=(
                f"chown -R {shlex.quote(agent_user)}:{shlex.quote(agent_user)} "
                f"{shlex.quote(_REMOTE_SOURCE_DIR)}"
            ),
        )
        return _REMOTE_SOURCE_DIR

    async def _prepare_mcp_config(self, environment: BaseEnvironment) -> None:
        if self._mcp_config_path is None:
            return
        staged = self.logs_dir / "paicli-mcp"
        if staged.exists():
            shutil.rmtree(staged)
        staged.mkdir(parents=True)
        shutil.copy2(self._mcp_config_path, staged / "mcp.json")
        await self.exec_as_root(
            environment,
            command=(
                "set -eu; "
                f"rm -rf {shlex.quote(_REMOTE_MCP_DIR)}; "
                f"mkdir -p {shlex.quote(_REMOTE_MCP_DIR)}"
            ),
        )
        await environment.upload_dir(staged, _REMOTE_MCP_DIR)

    def _stage_local_source(self) -> Path:
        source = self._source_dir
        if source is None:
            raise ValueError(
                "No local PaiCLI source directory is available. Pass source_dir=... "
                "or explicitly pass package=..."
            )
        required = (
            source / "pyproject.toml",
            source / "README.md",
            source / "src" / "paicli",
        )
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise ValueError("PaiCLI source directory is incomplete; missing " + ", ".join(missing))

        staged = self.logs_dir / "paicli-source"
        if staged.exists():
            shutil.rmtree(staged)
        (staged / "src").mkdir(parents=True)
        shutil.copy2(source / "pyproject.toml", staged / "pyproject.toml")
        shutil.copy2(source / "README.md", staged / "README.md")
        shutil.copytree(
            source / "src" / "paicli",
            staged / "src" / "paicli",
            ignore=_ignore_source_artifacts,
        )
        identity = _source_identity(source, staged)
        (staged / ".paicli-source-identity.json").write_text(
            json.dumps(identity, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return staged

    def _stage_package_identity(self) -> Path:
        staged = self.logs_dir / "paicli-metadata"
        if staged.exists():
            shutil.rmtree(staged)
        staged.mkdir(parents=True)
        identity = {
            "source": "package",
            "package": self._package,
            "package_sha256": hashlib.sha256((self._package or "").encode()).hexdigest(),
        }
        (staged / ".paicli-source-identity.json").write_text(
            json.dumps(identity, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return staged

    def _run_command(self, instruction: str) -> str:
        mcp_option = (
            f" --benchmark-mcp-config {shlex.quote(_REMOTE_MCP_DIR + '/mcp.json')}"
            if self._mcp_config_path is not None
            else ""
        )
        return (
            "set -eu; "
            f"export HOME={shlex.quote(_BENCHMARK_HOME)}; "
            f"mkdir -p {shlex.quote(_BENCHMARK_HOME)} {shlex.quote(_LOG_DIR)}; "
            f"exec {shlex.quote(_venv_python())} -m paicli "
            "--benchmark --cwd . "
            f"--prompt {shlex.quote(instruction)} "
            f"--benchmark-log-dir {shlex.quote(_LOG_DIR)} "
            f"--benchmark-source-identity {shlex.quote(self._remote_identity_path)} "
            f"--max-turns {self._max_turns} "
            f"--max-tool-calls {self._max_tool_calls} "
            f"--max-elapsed-seconds {self._max_elapsed_seconds} "
            f"{mcp_option}"
        )

    @staticmethod
    def _python_setup_command() -> str:
        return (
            "set -eu; "
            'PYTHON_BIN="python3"; '
            'MISSING_PACKAGES=""; '
            "if ! command -v python3 >/dev/null 2>&1; then "
            '  MISSING_PACKAGES="python3 python3-venv"; '
            "fi; "
            "if command -v python3 >/dev/null 2>&1 && ! python3 -c "
            "'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then "
            '  MISSING_PACKAGES="python3.11 python3.11-venv"; '
            "fi; "
            "if command -v python3.11 >/dev/null 2>&1; then PYTHON_BIN=python3.11; fi; "
            'VENV_PROBE="$(mktemp -d)"; '
            'if command -v "$PYTHON_BIN" >/dev/null 2>&1 && '
            '   (! "$PYTHON_BIN" -m venv "$VENV_PROBE/test-venv" >/dev/null 2>&1 || '
            '    ! "$VENV_PROBE/test-venv/bin/python" -m pip --version >/dev/null 2>&1); then '
            '  if [ "$PYTHON_BIN" = "python3.11" ]; then '
            '    MISSING_PACKAGES="$MISSING_PACKAGES python3.11-venv"; '
            "  else "
            '    MISSING_PACKAGES="$MISSING_PACKAGES python3-venv"; '
            "  fi; "
            "fi; "
            'rm -rf "$VENV_PROBE"; '
            'if [ -n "$MISSING_PACKAGES" ]; then '
            "  if ! apt-get -o Acquire::Retries=3 update; then "
            '    echo "apt update incomplete; attempting install from '
            'available signed indexes" >&2; '
            "  fi; "
            "  apt-get -o Acquire::Retries=3 install -y "
            "--no-install-recommends $MISSING_PACKAGES; "
            "fi; "
            'PYTHON_BIN=""; '
            "for candidate in python3.12 python3.11 python3; do "
            '  if command -v "$candidate" >/dev/null 2>&1 && '
            '     "$candidate" -c "import sys; raise SystemExit(sys.version_info < (3, 11))"; then '
            '    PYTHON_BIN="$(command -v "$candidate")"; break; '
            "  fi; "
            "done; "
            'if [ -z "$PYTHON_BIN" ]; then '
            '  echo "PaiCLI Harbor agent requires Python 3.11 or newer" >&2; exit 64; '
            "fi"
        )


def _default_source_dir() -> Path | None:
    root = Path(__file__).resolve().parents[2]
    return root if (root / "pyproject.toml").is_file() else None


def _ignore_source_artifacts(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name == "__pycache__" or name.endswith((".pyc", ".pyo"))}


def _source_identity(source: Path, staged: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    for path in sorted(item for item in staged.rglob("*") if item.is_file()):
        relative = path.relative_to(staged).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    identity: dict[str, object] = {
        "source": "local_checkout",
        "content_sha256": digest.hexdigest(),
        "commit": "",
        "branch": "",
        "dirty": False,
    }
    if not (source / ".git").exists():
        return identity
    identity["commit"] = _git_output(source, "rev-parse", "HEAD")
    identity["branch"] = _git_output(source, "branch", "--show-current")
    identity["dirty"] = bool(_git_output(source, "status", "--porcelain"))
    return identity


def _git_output(source: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=source,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _validate_mcp_config(path: Path) -> None:
    if not path.is_file():
        raise ValueError(f"MCP config file does not exist: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid MCP config: {path}") from exc
    servers = value.get("mcpServers") if isinstance(value, dict) else None
    if not isinstance(servers, dict):
        raise ValueError("MCP config must contain an mcpServers object")
    for name, raw in servers.items():
        if not isinstance(raw, dict):
            raise ValueError(f"MCP server config must be an object: {name}")
        for section in ("env", "headers"):
            values = raw.get(section) or {}
            if not isinstance(values, dict):
                raise ValueError(f"MCP {section} must be an object: {name}")
            if any(
                _is_credential_name(str(key)) and not _ENV_REFERENCE.fullmatch(str(item))
                for key, item in values.items()
            ):
                raise ValueError("MCP credentials must use ${NAME} environment references")


def _is_credential_name(name: str) -> bool:
    normalized = name.upper().replace("-", "_")
    return any(marker in normalized for marker in _CREDENTIAL_MARKERS)


def _install_command(install_spec: str) -> str:
    quoted_root = shlex.quote(_AGENT_ROOT)
    quoted_spec = shlex.quote(install_spec)
    return (
        "set -eu; "
        f"AGENT_ROOT={quoted_root}; "
        'PYTHON_BIN=""; '
        "for candidate in python3.12 python3.11 python3; do "
        '  if command -v "$candidate" >/dev/null 2>&1 && '
        '     "$candidate" -c "import sys; raise SystemExit(sys.version_info < (3, 11))"; then '
        '    PYTHON_BIN="$(command -v "$candidate")"; break; '
        "  fi; "
        "done; "
        'if [ -z "$PYTHON_BIN" ]; then '
        '  echo "PaiCLI requires Python 3.11 or newer" >&2; exit 64; '
        "fi; "
        "if command -v uv >/dev/null 2>&1; then "
        '  uv venv "$AGENT_ROOT/.venv" --python "$PYTHON_BIN" --clear; '
        '  uv pip install --python "$AGENT_ROOT/.venv/bin/python" --no-cache '
        f"{quoted_spec}; "
        "else "
        '  "$PYTHON_BIN" -m venv "$AGENT_ROOT/.venv" --clear; '
        '  "$AGENT_ROOT/.venv/bin/python" -m pip install --no-cache '
        f"{quoted_spec}; "
        "fi"
    )


def _venv_python() -> str:
    return f"{_AGENT_ROOT}/.venv/bin/python"


def _positive_int(value: int | str, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _positive_float(value: float | str, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be positive") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


__all__ = ["PaiCliHarborAgent"]
