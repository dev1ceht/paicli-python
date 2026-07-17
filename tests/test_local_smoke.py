from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


class _ScriptedWriteClient:
    provider_name = "scripted"
    model_name = "scripted-write"
    max_context_window = 128_000

    def __init__(self):
        self.calls = 0
        self.user_messages: list[str] = []

    async def chat(self, messages, tools, *, system_prompt):
        del tools, system_prompt
        self.calls += 1
        self.user_messages.extend(
            str(message.content) for message in messages if message.role == "user"
        )
        yield {
            "type": "context_usage",
            "state": "active",
            "scope": "agent",
            "estimated": self.calls == 1,
            "used_tokens": 50 if self.calls == 1 else 75,
            "pressure_ratio": 0.05 if self.calls == 1 else 0.075,
        }
        if self.calls == 1:
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": "write_value",
                    "function": {
                        "name": "write_file",
                        "arguments": (
                            '{"path":"module.py","content":"VALUE = 2\\n",'
                            '"overwrite":true}'
                        ),
                    },
                },
            }
            yield {"type": "usage", "usage": {"input_tokens": 11, "output_tokens": 7}}
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return
        yield {"type": "text_delta", "text": "Implemented the requested change."}
        yield {"type": "usage", "usage": {"input_tokens": 13, "output_tokens": 5}}
        yield {"type": "message_end", "stop_reason": "end_turn"}


class _SecretResponseClient:
    provider_name = "scripted"
    model_name = "scripted-secret"
    max_context_window = 128_000

    def __init__(self, secret: str):
        self.secret = secret

    async def chat(self, messages, tools, *, system_prompt):
        del messages, tools, system_prompt
        yield {"type": "text_delta", "text": f"credential {self.secret}"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


class _SecretPatchClient:
    provider_name = "scripted"
    model_name = "scripted-secret-patch"
    max_context_window = 128_000

    def __init__(self, secret: str):
        self.secret = secret
        self.calls = 0

    async def chat(self, messages, tools, *, system_prompt):
        del messages, tools, system_prompt
        self.calls += 1
        if self.calls == 1:
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": "write_secret",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "credential.txt", "content": self.secret}),
                    },
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return
        yield {"type": "message_end", "stop_reason": "end_turn"}


class _EnvironmentProbeClient:
    provider_name = "scripted"
    model_name = "scripted-environment-probe"
    max_context_window = 128_000

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools, *, system_prompt):
        del messages, tools, system_prompt
        self.calls += 1
        if self.calls == 1:
            command = (
                'python -c "import os,pathlib; '
                "pathlib.Path('leak.txt').write_text("
                "os.getenv('PAICLI_BENCHMARK_TEST_API_KEY','missing'))\""
            )
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": "probe_environment",
                    "function": {
                        "name": "execute_command",
                        "arguments": json.dumps({"command": command}),
                    },
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return
        yield {"type": "text_delta", "text": "Environment checked."}
        yield {"type": "message_end", "stop_reason": "end_turn"}


class _FailingClient:
    provider_name = "scripted"
    model_name = "scripted-failure"
    max_context_window = 128_000

    async def chat(self, messages, tools, *, system_prompt):
        del messages, tools, system_prompt
        raise RuntimeError("scripted provider failure")
        yield  # pragma: no cover - keeps this an async generator


class _InterruptingClient:
    provider_name = "scripted"
    model_name = "scripted-interrupt"
    max_context_window = 128_000

    async def chat(self, messages, tools, *, system_prompt):
        del messages, tools, system_prompt
        raise KeyboardInterrupt
        yield  # pragma: no cover - keeps this an async generator


class _ReferenceSolutionClient:
    provider_name = "scripted"
    model_name = "scripted-reference-solution"
    max_context_window = 128_000

    def __init__(self, task_id: str):
        self.writes = list(_REFERENCE_SOLUTIONS[task_id])
        self.calls = 0

    async def chat(self, messages, tools, *, system_prompt):
        del messages, tools, system_prompt
        self.calls += 1
        if self.writes:
            path, content = self.writes.pop(0)
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": f"reference_write_{self.calls}",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps(
                            {"path": path, "content": content, "overwrite": True}
                        ),
                    },
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return
        yield {"type": "text_delta", "text": "Reference solution applied."}
        yield {"type": "message_end", "stop_reason": "end_turn"}


_REFERENCE_SOLUTIONS: dict[str, list[tuple[str, str]]] = {
    "multi-file-refactor": [
        (
            "src/profile.py",
            'def get_user_name(user):\n    return user["name"].strip().title()\n',
        ),
        (
            "src/main.py",
            "from src.profile import get_user_name\n\n\n"
            'def greeting(user):\n    return f"Hello, {get_user_name(user)}"\n',
        ),
        (
            "src/report.py",
            "from src.profile import get_user_name\n\n\n"
            'def render_user(user):\n    return f"User: {get_user_name(user)}"\n',
        ),
        (
            "tests/test_public.py",
            "from src.main import greeting\n"
            "from src.profile import get_user_name\n"
            "from src.report import render_user\n\n\n"
            "def test_existing_name_behavior():\n"
            '    assert get_user_name({"name": " ada "}) == "Ada"\n'
            '    assert greeting({"name": "grace"}) == "Hello, Grace"\n'
            '    assert render_user({"name": "alan"}) == "User: Alan"\n',
        ),
    ],
    "debug-and-fix": [
        (
            "src/handler.py",
            "def process(values):\n    return sum(values)\n",
        )
    ],
    "health-endpoint": [
        (
            "app.py",
            'ROUTES = {"/": {"status": "running"}, "/health": {"status": "ok"}}\n\n\n'
            "def get(path):\n    return ROUTES[path], 200\n",
        ),
        (
            "tests/test_public.py",
            "from app import get\n\n\n"
            "def test_index_is_running():\n"
            '    assert get("/") == ({"status": "running"}, 200)\n\n\n'
            "def test_health_is_ok():\n"
            '    assert get("/health") == ({"status": "ok"}, 200)\n',
        ),
    ],
    "config-migration": [
        (
            f"services/{service}/config.yaml",
            f"schema_version: 2\nservice: {service}\n",
        )
        for service in ("api", "auth", "gateway", "worker")
    ],
    "dependency-upgrade": [
        ("requirements.txt", "requests==2.31\n"),
        (
            "src/client.py",
            "def build_session(session):\n    session.trust_env = False\n    return session\n",
        ),
        (
            "src/api.py",
            "import requests\n\n\ndef fetch(url):\n    return requests.get(url, timeout=10)\n",
        ),
        (
            "src/fetcher.py",
            "import requests\n\n\ndef fetch(url):\n    return requests.get(url, timeout=10)\n",
        ),
    ],
    "string-normalize": [
        (
            "src/text_tools.py",
            "import re\n\n\n"
            "def normalize_username(value: str) -> str:\n"
            '    normalized = re.sub(r"\\s+", "-", value.strip()).lower()\n'
            "    if not normalized:\n"
            '        raise ValueError("username cannot be blank")\n'
            "    return normalized\n",
        )
    ],
    "invoice-totals": [
        (
            "src/invoice.py",
            "from __future__ import annotations\n\n\n"
            "def invoice_total(items: list[dict], tax_rate: float = 0.0) -> float:\n"
            "    subtotal = 0.0\n"
            "    for item in items:\n"
            '        quantity = item.get("quantity", 1)\n'
            "        if quantity < 0:\n"
            '            raise ValueError("quantity cannot be negative")\n'
            '        subtotal += float(item["price"]) * quantity\n'
            "    return round(subtotal * (1 + tax_rate), 2)\n",
        )
    ],
}


def _write_suite(root: Path, *, task_overrides: dict | None = None) -> Path:
    fixture = root / "fixtures" / "demo"
    acceptance = root / "acceptance" / "demo"
    fixture.mkdir(parents=True)
    acceptance.mkdir(parents=True)
    (fixture / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (acceptance / "test_acceptance.py").write_text(
        "from module import VALUE\n\n\ndef test_value():\n    assert VALUE == 2\n",
        encoding="utf-8",
    )
    task = {
        "id": "demo",
        "prompt": "Change VALUE to 2.",
        "fixture_repo": "fixtures/demo",
        "acceptance": "acceptance/demo",
    }
    task.update(task_overrides or {})
    manifest = root / "tasks.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "suite_id": "local-smoke-v1",
                "verifier": {"kind": "pytest", "timeout_seconds": 120},
                "tasks": [task],
            }
        ),
        encoding="utf-8",
    )
    return manifest


def test_valid_local_smoke_manifest_loads_with_content_fingerprint(tmp_path: Path):
    from paicli.evaluation.local_smoke import load_local_smoke_suite

    manifest = _write_suite(tmp_path)

    suite = load_local_smoke_suite(manifest)

    assert suite.suite_id == "local-smoke-v1"
    assert suite.tasks[0].id == "demo"
    assert suite.tasks[0].fixture_repo == tmp_path / "fixtures" / "demo"
    assert suite.tasks[0].acceptance == tmp_path / "acceptance" / "demo"
    assert len(suite.fingerprint) == 64


def test_local_smoke_manifest_rejects_paths_outside_suite(tmp_path: Path):
    from paicli.evaluation.local_smoke import load_local_smoke_suite

    outside = tmp_path.parent / "outside-fixture"
    outside.mkdir(exist_ok=True)
    manifest = _write_suite(tmp_path, task_overrides={"fixture_repo": "../outside-fixture"})

    with pytest.raises(ValueError, match="inside the suite root"):
        load_local_smoke_suite(manifest)


def test_local_smoke_manifest_rejects_duplicate_task_ids(tmp_path: Path):
    from paicli.evaluation.local_smoke import load_local_smoke_suite

    manifest = _write_suite(tmp_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["tasks"].append(dict(data["tasks"][0]))
    manifest.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate task id: demo"):
        load_local_smoke_suite(manifest)


def test_local_smoke_manifest_rejects_task_id_path_traversal(tmp_path: Path):
    from paicli.evaluation.local_smoke import load_local_smoke_suite

    manifest = _write_suite(tmp_path, task_overrides={"id": "../escape"})

    with pytest.raises(ValueError, match="does not match"):
        load_local_smoke_suite(manifest)


def test_local_smoke_manifest_rejects_overlapping_fixture_and_acceptance(tmp_path: Path):
    from paicli.evaluation.local_smoke import load_local_smoke_suite

    manifest = _write_suite(
        tmp_path,
        task_overrides={"acceptance": "fixtures/demo"},
    )

    with pytest.raises(ValueError, match="must not overlap"):
        load_local_smoke_suite(manifest)


def test_local_smoke_manifest_rejects_fixture_git_metadata(tmp_path: Path):
    from paicli.evaluation.local_smoke import load_local_smoke_suite

    manifest = _write_suite(tmp_path)
    metadata = tmp_path / "fixtures" / "demo" / ".git"
    metadata.mkdir()
    (metadata / "config").write_text("[core]\n\thooksPath = hooks\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Git metadata"):
        load_local_smoke_suite(manifest)


def test_local_smoke_fingerprint_ignores_transient_python_cache(tmp_path: Path):
    from paicli.evaluation.local_smoke import load_local_smoke_suite

    manifest = _write_suite(tmp_path)
    original = load_local_smoke_suite(manifest).fingerprint
    cache = tmp_path / "fixtures" / "demo" / "__pycache__"
    cache.mkdir()
    (cache / "module.cpython-313.pyc").write_bytes(b"transient bytecode")

    assert load_local_smoke_suite(manifest).fingerprint == original


def test_benchmark_patch_includes_committed_and_untracked_final_changes(tmp_path: Path):
    from paicli.evaluation.local_smoke import (
        collect_benchmark_patch,
        load_local_smoke_suite,
        materialize_benchmark_workspace,
    )

    suite = load_local_smoke_suite(_write_suite(tmp_path / "suite"))
    workspace = materialize_benchmark_workspace(suite.tasks[0], tmp_path / "workspace")
    (workspace.path / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "module.py"], cwd=workspace.path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "fix"],
        cwd=workspace.path,
        check=True,
        capture_output=True,
        text=True,
    )
    (workspace.path / "created.py").write_text("CREATED = True\n", encoding="utf-8")

    patch = collect_benchmark_patch(workspace)

    assert "-VALUE = 1" in patch
    assert "+VALUE = 2" in patch
    assert "+CREATED = True" in patch


def test_benchmark_patch_includes_new_files_ignored_by_fixture(tmp_path: Path):
    from paicli.evaluation.local_smoke import (
        collect_benchmark_patch,
        load_local_smoke_suite,
        materialize_benchmark_workspace,
    )

    manifest = _write_suite(tmp_path / "suite")
    fixture = tmp_path / "suite" / "fixtures" / "demo"
    (fixture / ".gitignore").write_text("generated.txt\n", encoding="utf-8")
    suite = load_local_smoke_suite(manifest)
    workspace = materialize_benchmark_workspace(suite.tasks[0], tmp_path / "workspace")
    (workspace.path / "generated.txt").write_text("GENERATED = True\n", encoding="utf-8")

    patch = collect_benchmark_patch(workspace)

    assert "generated.txt" in patch
    assert "+GENERATED = True" in patch


def test_project_local_smoke_v1_contains_the_seven_confirmed_tasks():
    from paicli.evaluation.local_smoke import load_local_smoke_suite

    root = Path(__file__).resolve().parents[1]

    suite = load_local_smoke_suite(root / "benchmarks" / "local-smoke-v1" / "tasks.json")

    assert [task.id for task in suite.tasks] == [
        "multi-file-refactor",
        "debug-and-fix",
        "health-endpoint",
        "config-migration",
        "dependency-upgrade",
        "string-normalize",
        "invoice-totals",
    ]


def test_project_local_smoke_v1_accepts_reference_solutions(tmp_path: Path):
    from paicli.evaluation.local_smoke import run_local_smoke

    root = Path(__file__).resolve().parents[1]

    result = run_local_smoke(
        root / "benchmarks" / "local-smoke-v1" / "tasks.json",
        output_dir=tmp_path / "artifacts",
        client_factory=lambda task, repetition, config: _ReferenceSolutionClient(task.id),
    )

    assert len(result["attempts"]) == 7
    assert all(
        attempt["execution_status"] == "completed" and attempt["verification_status"] == "passed"
        for attempt in result["attempts"]
    )


def test_local_smoke_runs_production_agent_and_verifies_in_isolation(tmp_path: Path):
    from paicli.evaluation.local_smoke import run_local_smoke

    manifest = _write_suite(tmp_path / "suite")
    client = _ScriptedWriteClient()

    result = run_local_smoke(
        manifest,
        output_dir=tmp_path / "artifacts",
        client_factory=lambda task, repetition, config: client,
    )

    attempt = result["attempts"][0]
    assert attempt["execution_status"] == "completed"
    assert attempt["verification_status"] == "passed"
    assert attempt["actual_usage"] == {"input_tokens": 24, "output_tokens": 12, "total_tokens": 36}
    assert attempt["context_telemetry"] == {
        "estimated": {
            "max_used_tokens": 50,
            "max_pressure_ratio": 0.05,
            "samples": 1,
        },
        "provider_reported": {
            "max_used_tokens": 75,
            "max_pressure_ratio": 0.075,
            "samples": 1,
        },
    }
    assert attempt["context_reductions"] == 0
    assert attempt["tool_errors"] == 0
    assert result["configuration_identity"]["temperature"] == 0.0
    assert result["configuration_identity"]["tool_profile"] == ("network-tool-free-coding-v2")
    assert len(result["suite"]["fingerprint"]) == 64
    assert len(result["runtime_identity"]["fingerprint"]) == 64
    assert len(result["configuration_identity"]["fingerprint"]) == 64
    assert len(result["environment_identity"]["fingerprint"]) == 64
    assert result["isolation"]["filesystem_isolation"] is False
    assert result["isolation"]["network_isolation"] is False
    assert "+VALUE = 2" in (tmp_path / "artifacts" / attempt["patch_path"]).read_text(
        encoding="utf-8"
    )
    assert (tmp_path / "suite" / "fixtures" / "demo" / "module.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 1\n"
    assert client.user_messages[0] == "Change VALUE to 2."


def test_local_smoke_artifacts_redact_configured_credentials_from_model_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from paicli.evaluation.local_smoke import run_local_smoke

    secret = "plain-provider-secret-without-known-prefix"
    monkeypatch.setenv("PAICLI_API_KEY", secret)
    result = run_local_smoke(
        _write_suite(tmp_path / "suite"),
        output_dir=tmp_path / "artifacts",
        client_factory=lambda task, repetition, config: _SecretResponseClient(secret),
    )

    attempt = result["attempts"][0]
    attempt_dir = tmp_path / "artifacts" / Path(attempt["patch_path"]).parent
    persisted = (attempt_dir / "response.txt").read_text(encoding="utf-8") + (
        attempt_dir / "events.jsonl"
    ).read_text(encoding="utf-8")
    assert secret not in persisted
    assert "[REDACTED]" in persisted


def test_local_smoke_refuses_to_persist_configured_credential_in_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from paicli.evaluation.local_smoke import run_local_smoke

    secret = "plain-provider-secret-written-by-agent"
    monkeypatch.setenv("PAICLI_API_KEY", secret)

    result = run_local_smoke(
        _write_suite(tmp_path / "suite"),
        output_dir=tmp_path / "artifacts",
        client_factory=lambda task, repetition, config: _SecretPatchClient(secret),
    )

    attempt = result["attempts"][0]
    assert attempt["execution_status"] == "agent_error"
    assert attempt["verification_status"] == "not_run"
    assert attempt["patch_redacted"] is True
    for artifact in (tmp_path / "artifacts").rglob("*"):
        if artifact.is_file():
            assert secret.encode() not in artifact.read_bytes()


def test_local_smoke_refuses_to_persist_unconfigured_key_pattern_in_patch(tmp_path: Path):
    from paicli.evaluation.local_smoke import run_local_smoke

    secret = "sk-unconfiguredcredential12345"
    result = run_local_smoke(
        _write_suite(tmp_path / "suite"),
        output_dir=tmp_path / "artifacts",
        client_factory=lambda task, repetition, config: _SecretPatchClient(secret),
    )

    attempt = result["attempts"][0]
    assert attempt["execution_status"] == "agent_error"
    assert attempt["patch_redacted"] is True
    assert secret not in (tmp_path / "artifacts" / attempt["patch_path"]).read_text(
        encoding="utf-8"
    )


def test_local_smoke_shell_cannot_read_host_secret_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from paicli.evaluation.local_smoke import run_local_smoke

    secret = "plain-provider-secret-value"
    monkeypatch.setenv("PAICLI_BENCHMARK_TEST_API_KEY", secret)

    result = run_local_smoke(
        _write_suite(tmp_path / "suite"),
        output_dir=tmp_path / "artifacts",
        client_factory=lambda task, repetition, config: _EnvironmentProbeClient(),
    )

    attempt = result["attempts"][0]
    patch = (tmp_path / "artifacts" / attempt["patch_path"]).read_text(encoding="utf-8")
    assert secret not in patch
    assert "+missing" in patch
    assert os.environ["PAICLI_BENCHMARK_TEST_API_KEY"] == secret


def test_local_smoke_continues_after_agent_error_and_cleans_workspaces(tmp_path: Path):
    from paicli.evaluation.local_smoke import run_local_smoke

    suite_root = tmp_path / "suite"
    manifest = _write_suite(suite_root)
    shutil.copytree(suite_root / "fixtures" / "demo", suite_root / "fixtures" / "second")
    shutil.copytree(suite_root / "acceptance" / "demo", suite_root / "acceptance" / "second")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["tasks"].append(
        {
            "id": "second",
            "prompt": "Change VALUE to 2.",
            "fixture_repo": "fixtures/second",
            "acceptance": "acceptance/second",
        }
    )
    manifest.write_text(json.dumps(data), encoding="utf-8")

    result = run_local_smoke(
        manifest,
        output_dir=tmp_path / "artifacts",
        client_factory=lambda task, repetition, config: (
            _FailingClient() if task.id == "demo" else _ScriptedWriteClient()
        ),
    )

    assert [attempt["execution_status"] for attempt in result["attempts"]] == [
        "agent_error",
        "completed",
    ]
    assert [attempt["verification_status"] for attempt in result["attempts"]] == [
        "not_run",
        "passed",
    ]
    assert not list((tmp_path / "artifacts" / "runs").rglob("workspace"))


def test_local_smoke_interrupt_persists_metadata_and_cleans_workspace(tmp_path: Path):
    from paicli.evaluation.local_smoke import run_local_smoke

    output = tmp_path / "artifacts"
    with pytest.raises(KeyboardInterrupt):
        run_local_smoke(
            _write_suite(tmp_path / "suite"),
            output_dir=output,
            client_factory=lambda task, repetition, config: _InterruptingClient(),
        )

    metadata = json.loads(
        (output / "attempts" / "demo" / "0" / "metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["error"] == "KeyboardInterrupt"
    assert not list((output / "runs").rglob("workspace"))


def test_live_local_smoke_requires_explicit_unsandboxed_acknowledgement(tmp_path: Path):
    from paicli.evaluation.local_smoke import run_local_smoke

    with pytest.raises(ValueError, match="--allow-unsandboxed"):
        run_local_smoke(
            _write_suite(tmp_path / "suite"),
            output_dir=tmp_path / "artifacts",
        )


def test_local_smoke_verifies_preloaded_acceptance_material(tmp_path: Path):
    from paicli.evaluation.local_smoke import run_local_smoke

    manifest = _write_suite(tmp_path / "suite")

    def mutate_acceptance_after_preload(task, repetition, config):
        del repetition, config
        (task.acceptance / "test_acceptance.py").write_text(
            "def test_tampered():\n    assert False\n",
            encoding="utf-8",
        )
        return _ScriptedWriteClient()

    result = run_local_smoke(
        manifest,
        output_dir=tmp_path / "artifacts",
        repetitions=2,
        client_factory=mutate_acceptance_after_preload,
    )

    assert [attempt["verification_status"] for attempt in result["attempts"]] == [
        "passed",
        "passed",
    ]


def test_local_smoke_cli_maps_argument_errors_to_setup_exit_code():
    root = Path(__file__).resolve().parents[1]

    process = subprocess.run(
        [sys.executable, "scripts/evaluate_local_smoke.py", "--unknown-option"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert process.returncode == 1
    assert "argument error" in process.stderr


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [
        (
            [{"execution_status": "completed", "verification_status": "passed"}],
            0,
        ),
        (
            [{"execution_status": "benchmark_error", "verification_status": "not_run"}],
            1,
        ),
        (
            [{"execution_status": "agent_error", "verification_status": "not_run"}],
            2,
        ),
        (
            [{"execution_status": "completed", "verification_status": "failed"}],
            2,
        ),
    ],
)
def test_local_smoke_exit_code_distinguishes_infrastructure_and_task_failures(
    attempts: list[dict[str, str]], expected: int
):
    from paicli.evaluation.local_smoke import local_smoke_exit_code

    assert local_smoke_exit_code({"attempts": attempts}) == expected
