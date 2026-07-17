# SWE-bench Lite v1 Evaluation

PaiCLI will add a reproducible SWE-bench Lite evaluation pipeline for a controlled comparison between a full-history context baseline and the current optimized context manager. PaiCLI generates official-format predictions and telemetry; the user runs the official Docker harness manually; PaiCLI then imports the official outcomes and produces a paired pass@1 and provider input-token comparison.

## Goals

- Run real SWE-bench repository issues through PaiCLI's production `QueryEngine` and Agent path.
- Compare one full-history baseline with the current optimized context manager while holding every non-target identity fixed.
- Generate official-format `predictions.jsonl` files without launching the official harness.
- Import user-operated official harness results without reinterpreting their resolved decisions.
- Support an evidence-backed statement of pass@1 and average provider input-token change on a fixed task suite when the observed data satisfies the reporting gate.

## Non-goals

- PaiCLI will not install, launch, or orchestrate the official SWE-bench Docker harness.
- The first version will not reproduce a full SWE-bench Lite leaderboard run.
- The first version will not guarantee reference-data confidentiality, filesystem isolation, or network isolation during Agent shell execution.
- The first version will not install historical repository dependencies or provide a managed Agent test environment.
- The first version will not run prediction generation concurrently.
- Scripted clients, estimated token counts, Agent self-tests, and local patch checks will not be presented as official correctness or provider cost evidence.

## Pipeline

```text
fetch-dataset or import-dataset
  -> fingerprinted local SWE-bench Lite snapshot
  -> deterministic capability-30 selection
  -> deterministic context-stress-10 selection
  -> versioned context-stress-5-v1 formal subset
  -> prepare reusable bare repository mirrors
  -> generate one counterbalanced full-history/optimized experiment
  -> two official-format predictions files
  -> user-operated official harness runs
  -> report imports two complete official outcome sets
  -> compare produces the paired experiment report
```

The public script entry point is `scripts/evaluate_swebench.py` with six independent subcommands:

- `fetch-dataset`
- `import-dataset`
- `prepare`
- `generate`
- `report`
- `compare`

There is no `score`, `run`, or `--max-workers` interface. Prediction generation is always serial. Commands return `0` when they produce a complete valid stage artifact, including valid runs with Agent failures or unresolved tasks; invalid pipeline inputs or infrastructure return `1`, and user interruption returns `130`.

## Version-controlled definitions

```text
benchmarks/swebench-lite-v1/
  selections/
    capability-30.json
    context-stress-10.json
    context-stress-5-v1.json
    flask-pilot-1-v1.json
  profiles/
    stress-32k-v1.json
    swe-lite-agent-v1.json
    qwen3.6-flash-temp0-v1.json
```

Selection files contain identities, seeds, ordered instance IDs, source-dataset identity, and content fingerprints rather than the full official dataset. Profile files are immutable inputs to formal runs. An ad hoc CLI override is identified as a custom development configuration and cannot retain or aggregate with a named formal profile identity.

## Dataset snapshot and task projection

`fetch-dataset` requires explicit network authorization and the optional `swebench` dependency extra containing Hugging Face `datasets`. `import-dataset` uses an existing local JSON source and requires no Hugging Face dependency. Both produce the same normalized dataset metadata, source attribution, revision when available, and SHA-256 fingerprint.

The complete local data snapshot remains under ignored `artifacts/` and is supplied to the user-operated official harness. PaiCLI generation projects only these fields from each source record:

- `instance_id`
- `repo`
- `base_commit`
- `problem_statement`

Other official fields may exist in the source JSON but are ignored, are not sent to the Agent, and are not copied into generation artifacts. Because Agent shell execution is not filesystem-isolated, projection does not establish reference-data confidentiality; artifacts record `reference_data_confidentiality=false`.

## Fixed task sets

`capability-30` is selected from a pinned SWE-bench Lite snapshot before model execution. Instances are grouped by upstream repository, ordered within each group by a stable hash of the published seed `paicli-capability-30-v1` and instance identity, and taken in repository-balanced rounds until 30 are fixed.

`context-stress-10` is deterministically derived from `capability-30` using the independent seed `paicli-context-stress-10-v1` and the same repository-round rule. `context-stress-5-v1` freezes the first five ordered instances from that already-selected population. Neither set may use gold patches, test answers, PaiCLI outcomes, token usage, compression observations, or post-run replacement. Context pressure comes from the named budget profile, not result-based task selection.

The first formal comparison uses only `context-stress-5-v1`. Results must be described as evidence from that fixed five-task SWE-bench Lite context-pressure subset under the named 32K profile, not as a full SWE-bench Lite score. `flask-pilot-1-v1` is a one-task real-model development pilot excluded from the formal task set and claim.

## Repository preparation

`prepare` maintains one bare Git mirror per validated `owner/name` upstream repository. It uses existing mirrors by default; clone or fetch requires explicit network authorization. Remote URLs are derived from the validated repository identity rather than accepted as arbitrary dataset input.

Preparation verifies that every selected `base_commit` exists. With network authorization it fetches an existing mirror only when the required base commit is absent. Prediction generation performs no source acquisition. Each instance, context variant, and repetition receives an independent ordinary clone from the mirror, checked out detached at the exact base commit. Before any Agent starts, the runner preflights all selected commits and clean checkouts.

On Windows, every Git subprocess uses command-local `core.longpaths=true`; global Git configuration is never mutated. Generation and apply-check use short hashed temporary checkout paths so repository filenames do not exceed the host path limit.

The cache contains Git history only. Agent modifications, indexes, sessions, audits, tool-result storage, snapshots, and workspaces are never shared between variants. Workspaces are deleted after patch and artifact collection by default; development runs may explicitly retain them.

## Formal experiment identities

Every generation run records and fingerprints separate identities for:

- dataset snapshot and fixed subset;
- PaiCLI version, clean source revision, and source-content fingerprint;
- provider, model, temperature, maximum output, and base-URL hash;
- Agent resource profile;
- context budget profile and context variant;
- tool profile;
- host operating system, architecture, Python, and relevant dependency versions;
- original problem-statement hashes.

The formal A/B comparison permits only `context_identity.variant` to differ. Dataset and subset fingerprints, runtime, model settings, Agent budget, context-budget values, tools, prompts, and environment must match exactly. Both variants run from the same clean PaiCLI commit; a dirty runtime is rejected before any formal model call.

## Model and Agent profiles

The first formal model profile is:

```json
{
  "provider": "qwen",
  "model": "qwen3.6-flash",
  "temperature": 0,
  "max_output_tokens": 4096
}
```

The implementation supports other configured models through new configuration identities. API keys are never fingerprinted or stored; the base URL is stored only as a hash. Artifacts also record provider/model identity reported by the live client when available.

The first Agent resource profile is:

```json
{
  "profile_id": "swe-lite-agent-v1",
  "max_turns": 60,
  "max_tool_calls": 100,
  "max_elapsed_seconds": 1800,
  "max_total_tokens": 300000
}
```

Both variants use the same resource profile. These cumulative guards are independent of the per-request context input budget.

## Context profile and variants

The initial formal context profile is configurable in implementation but immutable as a named profile:

```json
{
  "profile_id": "stress-32k-v1",
  "input_budget_tokens": 32768,
  "output_reserve_tokens": 4096
}
```

Changing either value creates a new profile identity, such as a later 64K profile; results from different profiles are not aggregated. Each individual outbound model request is subject to the same profile budget in both variants. Cumulative task usage may exceed the per-request budget.

The full-history baseline preserves conversation messages and tool results without offloading, compression, pruning, or summarization. It still produces equivalent context telemetry and enforces the same input budget; an oversized request terminates with `context_limit_exceeded`.

The optimized variant uses the current production `ContextManager` with the uniform profile and no task-specific tuning. It retains the production reduction sequence, protected-turn behavior, model-assisted summary strategy, and deterministic fallback. Every model-assisted summary call is attributed to the optimized attempt's provider usage.

Both variants use the same production `QueryEngine` and Agent loop. A narrow context-manager factory seam selects the full-history or optimized manager during benchmark Agent construction; normal PaiCLI behavior continues to construct the optimized production manager by default. The benchmark must not bypass `QueryEngine` to call the lower-level query loop.

## Prompt and tools

The Agent user message is the original `problem_statement` verbatim. Instance ID, repository, base commit, benchmark labels, gold data, and benchmark wrapper instructions do not enter the prompt.

Both variants use `network-tool-free-coding-v2`: `read_file`, safe new-file `write_file`, exact-block `edit_file`, structured `apply_patch`, search/list tools, and one platform-explicit `execute_command`. Existing files require `write_file(overwrite=true)` or a dedicated editing tool. Windows commands run through non-interactive Windows PowerShell 5.1, not Bash; POSIX commands run through `/bin/sh`. Dedicated web, browser, MCP, skills, long-term memory, and snapshot restoration are excluded. Generation does not install repository dependencies and does not acquire source code or packages. The Agent may run local tests in the unmanaged host environment, but those results are non-authoritative. Artifacts record:

```json
{
  "filesystem_isolation": false,
  "network_isolation": false,
  "reference_data_confidentiality": false,
  "agent_test_environment": "host_unmanaged"
}
```

## Generation scheduling and lifecycle

The first claim-eligible experiment uses one repetition: 5 fixed tasks, two variants, and 10 scheduled attempts. Generation is serial and counterbalanced by fixed task position:

```text
task 0: full-history, optimized
task 1: optimized, full-history
task 2: full-history, optimized
...
```

Every attempt receives a fresh model client, Agent session, tool storage, and workspace. Provider retry behavior is a fixed, fingerprinted transport policy inside the same model request. Whole-task regeneration after an Agent error, empty patch, context-limit failure, or poor result is prohibited.

Attempts are journaled as `not_started -> model_running -> generation_frozen -> completed|agent_error`. The frozen boundary atomically persists patch, response, safe events, provider usage, and hashes before local apply-check. Resume skips terminal attempts, completes apply-check for `generation_frozen` without another model call, and terminalizes stale `model_running` as `interrupted` with an empty patch. It never resamples an interrupted task. An experiment lock rejects concurrent active generation. The earlier `experiment-001` remains diagnostic and is not migrated.

## Patch and predictions

The benchmark patch is the complete final-tree Git diff from the declared base commit, including committed, staged, unstaged, untracked, deleted, and binary changes while excluding PaiCLI runtime files and common test caches. Agent staging and commit behavior do not affect the result.

Each variant emits one official-format row per scheduled instance:

```json
{
  "instance_id": "...",
  "model_name_or_path": "qwen3.6-flash",
  "model_patch": "diff --git ..."
}
```

Empty or failed Agent changes still receive a row with an empty patch so the fixed denominator cannot shrink. A local clean-base apply check is diagnostic only; the official harness owns patch-application and resolved outcomes. Credential rejection scans only added patch lines for the configured API key or high-confidence bearer, `sk-`, or private-key patterns. Broad redaction remains active for logs and events, but ordinary code identifiers such as `password = ReadOnlyPasswordHashField(...)` do not block a patch. A rejected patch records only its credential category, never its value, emits an empty prediction, and records `patch_status=credential_blocked`.

## Artifacts

Ignored run artifacts use this shape:

```text
artifacts/swebench-lite/
  datasets/<dataset-fingerprint>/
  repo-cache/<owner>__<repo>.git/
  runs/<experiment-id>/
    experiment.json
    harness-request.json
    harness-command.txt
    full-history/
      predictions.jsonl
      generation-results.json
      attempts/<instance-id>/
        metadata.json
        patch.diff
        response.txt
        events.jsonl
        context-events.jsonl
        local-apply-check.log
    optimized/
      predictions.jsonl
      generation-results.json
      attempts/<instance-id>/...
    imported-harness-results/
    comparison.json
    report.md
```

The generation step emits two separate suggested harness commands and a harness request containing dataset, subset, prediction, and expected-instance fingerprints. PaiCLI never executes those commands.

Artifacts store redacted final responses, safe tool-event summaries, actual provider usage, request budgets and pressure, context reduction before/after metrics, patch data, and diagnostic status. They omit API keys, authorization headers, raw HTTP requests, private reasoning/thinking, full message arrays, and unbounded tool output. Workspaces are not retained by default.

## User-operated official evaluation

The user runs the two prediction files through the official harness against the same local dataset snapshot and fixed instance IDs. The exact command, dataset fingerprint, prediction fingerprint, run ID, and official harness package version or source revision form the harness identity.

`report` receives the relevant official harness run directory and recursively reads per-instance `report.json` files without modifying them. It rejects missing, duplicate, or unexpected instance outcomes. A development import may record an unknown harness version; a formal consolidated report requires an exact harness identity, and the two variants must use the same identity.

An official resolved outcome is a pass. Official not-resolved, Agent error, context-limit failure, and empty patch are failures in the fixed denominator. Docker build, harness execution, or report infrastructure failures leave the official outcome set incomplete and must be retried against the same frozen predictions; they do not justify deleting or replacing a task.

## Metrics and comparison gate

With one generated patch per task:

```text
pass@1 = official resolved instances / 5 scheduled instances

average provider input tokens per task =
  provider-reported input tokens from every attributable model call
  / 5 scheduled instances

input-token reduction =
  (baseline average - optimized average) / baseline average
```

The input-token metric includes resolved and unresolved attempts and includes optimized model-assisted summary calls. Estimated context usage remains separate and cannot replace missing provider usage. Secondary metrics include output and total tokens, tokens per resolved task, common-success paired token differences, elapsed time, turns, tool errors, context-limit failures, peak pressure, and reduction actions.

The report displays pass@1 change in percentage points and token reduction as a percentage, plus per-instance paired results. It renders a suggested resume statement only when:

- both variants contain the same complete scheduled task set;
- both contain complete official outcomes;
- every attributable provider input usage is present, including summary calls;
- only the context variant identity differs;
- optimized pass@1 is higher; and
- optimized average provider input-token cost is lower.

Otherwise the report presents the observed data without claiming improvement.

## Automated verification

Ordinary tests make no network calls, clone no GitHub repositories, call no real model, and launch no Docker harness. They use temporary local Git repositories and deterministic scripted model clients while retaining the production `QueryEngine`, real tools, Git patching, artifact paths, and report logic.

Tests cover:

- complete-source JSON projection and deterministic subset derivation;
- dataset and selection fingerprints;
- safe repository identities and paths;
- explicit network gates, mirror reuse, commit verification, and dirty-workspace rejection;
- immutable profile validation and custom-override identity;
- production-path context-manager injection;
- no-reduction baseline behavior and shared input-budget enforcement;
- optimized reduction telemetry and summary-call usage attribution;
- complete patch capture and credential rejection;
- official prediction formatting;
- atomic attempt lifecycle, immutable output, and restricted resume;
- official harness directory imports and completeness checks;
- controlled-comparison identity validation;
- primary metric formulas and resume-statement gate; and
- redaction and default workspace cleanup.

## Manual acceptance

After automated verification:

1. Fetch or import a real pinned SWE-bench Lite snapshot.
2. Prepare one real instance and verify its mirror, base commit, and clean workspace.
3. Generate both variants for a one-instance development selection with the configured real model.
4. Validate the official harness environment separately with the official gold prediction.
5. Run the two generated prediction files manually through the same official harness revision.
6. Import both official run directories and verify the consolidated report.
7. Only then create a clean formal `context-stress-5-v1` experiment and run its 10 serial attempts.
