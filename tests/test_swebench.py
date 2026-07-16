from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

import paicli.evaluation.swebench as swebench_module
from paicli.config import PaiCliConfig
from paicli.context import ContextWindowExceededError
from paicli.evaluation.swebench import (
    ContextStressProfile,
    SweBenchInstance,
    compare_swebench_experiment,
    freeze_swebench_selection_manifests,
    full_history_context_manager_factory,
    import_swebench_dataset,
    import_swebench_harness_results,
    load_context_stress_profile,
    load_swebench_instances,
    materialize_swebench_workspace,
    prepare_swebench_repositories,
    run_swebench_generation,
    select_repository_balanced_instances,
)
from paicli.llm.base import PreparedOutboundRequest
from paicli.prompt import PromptSections
from paicli.types import Message


class _ScriptedWriteClient:
    provider_name = "scripted"
    model_name = "scripted-write"
    max_context_window = 36_864

    def __init__(self) -> None:
        self.calls = 0

    def prepare_request(self, messages, tools, *, system_prompt):
        del tools
        size = sum(len(str(message.content)) for message in messages) + len(system_prompt)
        return PreparedOutboundRequest(b"{}", estimated_input_tokens=size)

    async def chat(self, messages, tools, *, system_prompt):
        del messages, tools, system_prompt
        self.calls += 1
        if self.calls == 1:
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": "write_value",
                    "function": {
                        "name": "write_file",
                        "arguments": '{"path":"module.py","content":"VALUE = 2\\n"}',
                    },
                },
            }
            yield {"type": "usage", "usage": {"input_tokens": 11, "output_tokens": 7}}
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "usage", "usage": {"input_tokens": 13, "output_tokens": 5}}
        yield {"type": "message_end", "stop_reason": "end_turn"}


def test_load_swebench_instances_projects_generation_fields(tmp_path: Path) -> None:
    source = tmp_path / "official.jsonl"
    source.write_text(
        json.dumps(
            {
                "instance_id": "sympy__sympy-20590",
                "repo": "sympy/sympy",
                "base_commit": "abc123",
                "problem_statement": "Fix sympification.",
                "patch": "gold must not enter the task projection",
                "FAIL_TO_PASS": ["test_hidden"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert load_swebench_instances(source) == (
        SweBenchInstance(
            instance_id="sympy__sympy-20590",
            repo="sympy/sympy",
            base_commit="abc123",
            problem_statement="Fix sympification.",
        ),
    )


def test_select_repository_balanced_instances_takes_one_per_repo_first() -> None:
    instances = (
        SweBenchInstance("alpha__one-2", "alpha/one", "2", "two"),
        SweBenchInstance("alpha__one-1", "alpha/one", "1", "one"),
        SweBenchInstance("beta__two-1", "beta/two", "3", "three"),
        SweBenchInstance("gamma__three-1", "gamma/three", "4", "four"),
    )

    selected = select_repository_balanced_instances(
        instances,
        count=3,
        seed="paicli-capability-30-v1",
    )

    assert {item.repo for item in selected} == {
        "alpha/one",
        "beta/two",
        "gamma/three",
    }
    assert len({item.repo for item in selected}) == len(selected)


def test_load_context_stress_profile_records_exact_budget_and_fingerprint(
    tmp_path: Path,
) -> None:
    path = tmp_path / "stress-32k-v1.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile_id": "stress-32k-v1",
                "input_budget_tokens": 32768,
                "output_reserve_tokens": 4096,
            }
        ),
        encoding="utf-8",
    )

    profile = load_context_stress_profile(path)

    assert profile == ContextStressProfile(
        profile_id="stress-32k-v1",
        input_budget_tokens=32768,
        output_reserve_tokens=4096,
        fingerprint=profile.fingerprint,
    )
    assert len(profile.fingerprint) == 64


def test_prepare_repositories_reuses_mirror_and_materializes_clean_workspace(
    tmp_path: Path,
) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q")
    (upstream / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(upstream, "add", ".")
    _git(
        upstream,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-q",
        "-m",
        "base",
    )
    commit = _git(upstream, "rev-parse", "HEAD").stdout.strip()
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    mirror = cache_root / "example__repo.git"
    subprocess.run(
        ["git", "clone", "--mirror", str(upstream), str(mirror)],
        check=True,
        text=True,
        capture_output=True,
    )
    instance = SweBenchInstance("example__repo-1", "example/repo", commit, "Change it")

    prepared = prepare_swebench_repositories((instance,), cache_root=cache_root)
    workspace = materialize_swebench_workspace(
        instance,
        cache_root=cache_root,
        destination=tmp_path / "workspace",
    )

    assert prepared[0].base_commit == commit
    assert workspace.base_commit == commit
    assert _git(workspace.path, "status", "--porcelain").stdout == ""
    assert _git(workspace.path, "rev-parse", "HEAD").stdout.strip() == commit


def test_full_history_context_manager_preserves_messages_and_enforces_budget(
    tmp_path: Path,
) -> None:
    class Client:
        max_context_window = 1_000_000

        def prepare_request(self, messages, tools, *, system_prompt):
            del tools
            size = sum(len(str(message.content)) for message in messages) + len(system_prompt)
            return PreparedOutboundRequest(b"{}", estimated_input_tokens=size)

    profile = ContextStressProfile("stress-test", 20, 4, "f" * 64)
    manager = full_history_context_manager_factory(profile)(
        config=PaiCliConfig(),
        llm_client=Client(),
        cwd=str(tmp_path),
    )
    messages = [Message(role="user", content="12345")]

    result = asyncio.run(
        manager.build_turn_context(
            prompt_sections=PromptSections(prefix="system"),
            messages=messages,
            tools=[],
        )
    )

    assert result.messages == messages
    assert result.reductions == []
    assert result.prepared is not None
    assert result.prepared.quality_budget_tokens == 20
    with pytest.raises(ContextWindowExceededError):
        asyncio.run(
            manager.build_turn_context(
                prompt_sections=PromptSections(prefix="system prompt is too large"),
                messages=messages,
                tools=[],
            )
        )


def test_generation_runs_both_variants_through_production_agent(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream-generation"
    upstream.mkdir()
    _git(upstream, "init", "-q")
    (upstream / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(upstream, "add", ".")
    _git(
        upstream,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-q",
        "-m",
        "base",
    )
    commit = _git(upstream, "rev-parse", "HEAD").stdout.strip()
    cache_root = tmp_path / "generation-cache"
    cache_root.mkdir()
    subprocess.run(
        ["git", "clone", "--mirror", str(upstream), str(cache_root / "example__repo.git")],
        check=True,
        text=True,
        capture_output=True,
    )
    instance = SweBenchInstance("example__repo-1", "example/repo", commit, "Set VALUE to 2")
    profile = ContextStressProfile("stress-test", 32768, 4096, "f" * 64)

    result = run_swebench_generation(
        (instance,),
        cache_root=cache_root,
        output_dir=tmp_path / "experiment",
        context_profile=profile,
        client_factory=lambda _instance, _variant, _config: _ScriptedWriteClient(),
        formal=False,
    )
    resumed = run_swebench_generation(
        (instance,),
        cache_root=cache_root,
        output_dir=tmp_path / "experiment",
        context_profile=profile,
        client_factory=lambda _instance, _variant, _config: pytest.fail(
            "completed attempts must not run again"
        ),
        formal=False,
    )

    assert [attempt["variant"] for attempt in result["attempts"]] == [
        "full-history",
        "optimized",
    ]
    assert resumed["attempts"] == result["attempts"]
    for variant in ("full-history", "optimized"):
        prediction = json.loads(
            (tmp_path / "experiment" / variant / "predictions.jsonl").read_text(encoding="utf-8")
        )
        assert prediction["instance_id"] == instance.instance_id
        assert "VALUE = 2" in prediction["model_patch"]


def test_harness_import_requires_exact_complete_instance_set(tmp_path: Path) -> None:
    experiment = tmp_path / "experiment"
    experiment.mkdir()
    (experiment / "experiment.json").write_text(
        json.dumps({"instance_ids": ["task-1", "task-2"]}), encoding="utf-8"
    )
    for variant in ("full-history", "optimized"):
        prediction = experiment / variant / "predictions.jsonl"
        prediction.parent.mkdir(parents=True)
        prediction.write_text("{}\n", encoding="utf-8")
    harness = tmp_path / "harness"
    for instance_id, resolved in (("task-1", True), ("task-2", False)):
        report = harness / instance_id / "report.json"
        report.parent.mkdir(parents=True)
        report.write_text(json.dumps({instance_id: {"resolved": resolved}}), encoding="utf-8")

    imported = import_swebench_harness_results(
        experiment,
        variant="optimized",
        harness_results_dir=harness,
        harness_revision="swebench@abc123",
        formal=False,
    )

    assert imported["outcomes"] == {"task-1": True, "task-2": False}
    assert imported["harness_identity"]["revision"] == "swebench@abc123"
    assert (experiment / "imported-harness-results" / "optimized.json").exists()

    (harness / "task-2" / "report.json").unlink()
    with pytest.raises(ValueError, match="missing.*task-2"):
        import_swebench_harness_results(
            experiment,
            variant="full-history",
            harness_results_dir=harness,
            harness_revision="swebench@abc123",
            formal=False,
        )


def test_compare_uses_fixed_denominator_and_provider_input_usage(tmp_path: Path) -> None:
    experiment = tmp_path / "experiment"
    experiment.mkdir()
    attempts = []
    for variant, usages in (("full-history", (100, 100)), ("optimized", (70, 70))):
        for index, input_tokens in enumerate(usages, start=1):
            instance_id = f"task-{index}"
            patch_path = f"{variant}/attempts/{instance_id}/patch.diff"
            attempt = {
                "instance_id": instance_id,
                "variant": variant,
                "state": "completed",
                "patch_bytes": 5,
                "patch_path": patch_path,
                "patch_sha256": hashlib.sha256(b"patch").hexdigest(),
                "actual_usage": {"input_tokens": input_tokens, "output_tokens": 5},
                "usage_source": "provider_reported",
            }
            attempts.append(attempt)
            attempt_dir = experiment / variant / "attempts" / instance_id
            attempt_dir.mkdir(parents=True)
            (attempt_dir / "patch.diff").write_text("patch", encoding="utf-8")
            (attempt_dir / "metadata.json").write_text(json.dumps(attempt), encoding="utf-8")
    (experiment / "experiment.json").write_text(
        json.dumps(
            {
                "formal": True,
                "instance_ids": ["task-1", "task-2"],
                "attempts": attempts,
                "configuration_identity": {"model": "example/model"},
                "context_profile": {"profile_id": "stress-32k-v1"},
                "dataset_identity": {
                    "dataset_fingerprint": "a" * 64,
                    "selection_id": "context-stress-10",
                    "snapshot_dir": str(tmp_path / "snapshot"),
                },
            }
        ),
        encoding="utf-8",
    )
    for variant in ("full-history", "optimized"):
        prediction = experiment / variant / "predictions.jsonl"
        prediction.parent.mkdir(exist_ok=True)
        prediction.write_text('{"instance_id":"task-1","model_patch":"patch"}\n', encoding="utf-8")
    (experiment / "harness-request.json").write_text(
        json.dumps(
            {
                "variants": {
                    variant: {
                        "predictions_sha256": hashlib.sha256(
                            (experiment / variant / "predictions.jsonl").read_bytes()
                        ).hexdigest(),
                        "run_id": swebench_module._expected_harness_run_id(experiment, variant),
                    }
                    for variant in ("full-history", "optimized")
                }
            }
        ),
        encoding="utf-8",
    )
    for variant, outcomes in (
        ("full-history", {"task-1": True, "task-2": False}),
        ("optimized", {"task-1": True, "task-2": True}),
    ):
        harness = tmp_path / swebench_module._expected_harness_run_id(experiment, variant)
        for instance_id, resolved in outcomes.items():
            report_path = harness / instance_id / "report.json"
            report_path.parent.mkdir(parents=True)
            report_path.write_text(
                json.dumps({instance_id: {"resolved": resolved}}), encoding="utf-8"
            )
        import_swebench_harness_results(
            experiment,
            variant=variant,
            harness_results_dir=harness,
            harness_revision="swebench@abc123",
        )

    comparison = compare_swebench_experiment(experiment)

    assert comparison["variants"]["full-history"]["pass_at_1"] == 0.5
    assert comparison["variants"]["optimized"]["pass_at_1"] == 1.0
    assert comparison["input_token_reduction"] == pytest.approx(0.3)
    assert comparison["claim_eligible"] is True
    assert "从 50.0% 提升至 100.0%" in comparison["suggested_resume_statement"]
    assert len(comparison["paired_results"]) == 2
    assert (experiment / "report.md").exists()


def test_dataset_import_preserves_source_and_writes_fixed_selections(tmp_path: Path) -> None:
    source = tmp_path / "lite.json"
    source.write_text(
        json.dumps(
            [
                {
                    "instance_id": f"repo{index}__task",
                    "repo": f"owner/repo{index}",
                    "base_commit": f"commit-{index}",
                    "problem_statement": f"problem {index}",
                    "patch": "gold secret",
                    "test_patch": "tests secret",
                }
                for index in range(4)
            ]
        ),
        encoding="utf-8",
    )

    snapshot = import_swebench_dataset(
        source,
        output_root=tmp_path / "datasets",
        capability_count=3,
        stress_count=2,
    )

    snapshot_dir = Path(snapshot["snapshot_dir"])
    assert snapshot["source"]["kind"] == "local-import"
    assert len(snapshot["selections"]["capability-30"]) == 3
    assert set(snapshot["selections"]["context-stress-10"]).issubset(
        snapshot["selections"]["capability-30"]
    )
    copied = json.loads((snapshot_dir / "dataset.json").read_text(encoding="utf-8"))
    assert copied[0]["patch"] == "gold secret"
    assert "gold secret" not in (snapshot_dir / "metadata.json").read_text(encoding="utf-8")
    manifests = freeze_swebench_selection_manifests(
        snapshot, manifest_root=tmp_path / "fixed-selections"
    )
    assert {path.name for path in manifests} == {
        "capability-30.json",
        "context-stress-10.json",
    }


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )
