# Local Smoke v2 Context-Pressure Evaluation

PaiCLI provides a self-contained seven-task local coding benchmark that exercises the
production Agent path. The files remain under `benchmarks/local-smoke-v1` for continuity,
while the immutable experiment identity is `local-smoke-v2`. It is an end-to-end local
comparison suite, not a broad coding-capability leaderboard.

## Suite

- `benchmarks/local-smoke-v1/tasks.json` is a strict JSON manifest with
  `schema_version: 2`, suite ID `local-smoke-v2`, one fixed pytest verifier definition,
  and seven uniquely identified tasks.
- Five tasks are adapted copies of PaiCLI's long-session scenarios. `string-normalize` and `invoice-totals` are adapted from FirstCoder revision `0067930`; the source projects and their original fixtures remain unchanged.
- Each task references a normal fixture directory and a separate acceptance directory beneath the suite root. Paths must be safe relative paths, exist, remain inside the suite, and not overlap.
- Each task also references a committed structured history and declares a pressure class.
  Histories contain task-relevant user, assistant, tool-call, and tool-result messages;
  they may not reference acceptance paths. Tool calls and results must be paired.
- Suite identity is a deterministic fingerprint over the normalized manifest plus every referenced fixture and acceptance file. A semantic task change requires a new suite version.

## Context-pressure design

- The fixed profile is `stress-16k-v1`: 16,384 input tokens with a 4,096-token
  output reserve. PaiCLI's production 50%, 70%, and 90% reduction thresholds are
  unchanged.
- `health-endpoint` and `string-normalize` are normal-pressure controls.
- `multi-file-refactor` and `debug-and-fix` are medium-pressure tasks designed to
  exercise old-tool-result pruning.
- `config-migration`, `dependency-upgrade`, and `invoice-totals` are high-pressure
  tasks designed to exercise summary reduction.
- `scripts/build_local_smoke_histories.py` deterministically rebuilds the committed
  histories without reading withheld acceptance material. Rebuilding or editing a
  history changes the suite fingerprint and therefore creates a different experiment.

## Execution

- The task prompt is sent verbatim through PaiCLI's production `QueryEngine`/`Agent` path. No benchmark-only prompt wrapper is added.
- Every attempt starts from a fresh copied fixture, initialized as a Git repository with a recorded base commit and a fresh Agent session.
- The baseline tool profile includes workspace file read/write/list/search and shell tools, while excluding web, browser, MCP, skill, memory, and snapshot-restoration tools.
- Benchmark configuration uses `temperature=0`, `hitl_mode=never`, and the production Agent limits: 20 turns, 40 tool calls, 600 seconds, and 100000 tokens. Tasks cannot override these limits.
- Live execution requires explicit `allow_unsandboxed`; artifacts record `filesystem_isolation=false`, `network_isolation=false`, and whether the risk was acknowledged. This suite is not safe for untrusted models or tasks.
- Attempts run serially. Repetitions default to one; formal comparisons should use three. Every repetition gets a fresh workspace and client.
- Context comparison schedules both variants for each task/repetition and alternates
  their order to counterbalance drift. `full-history` uses a benchmark-only no-reduction
  context manager; `optimized` uses the production `ContextManager`.
- Exactly one process may own an output directory. A terminated model call becomes a
  terminal `agent_error`; it is never resampled. A frozen generation may resume only by
  rerunning its independent verifier.

## Verification

- The runner freezes every fixture and acceptance file while loading and fingerprinting the suite, before any Agent starts. All repetitions materialize from those frozen bytes. This guarantees acceptance integrity, not confidentiality from arbitrary unsandboxed shell access.
- The benchmark patch is the net difference from the recorded base tree to the final file tree, including committed, staged, unstaged, deleted, and untracked changes.
- Verification creates a fresh copy of the fixture, applies the benchmark patch, overlays the preloaded acceptance files, then runs `[sys.executable, "-m", "pytest", "-q"]` with `shell=False` and a 120-second timeout.
- No fixture dependency installation occurs. Acceptance tests use only Python's standard library and pytest.

## Results and artifacts

- Execution status is `completed`, `agent_error`, or `benchmark_error`; verification status is `passed`, `failed`, or `not_run`. A task error does not stop later attempts.
- Correctness is the primary result. Provider-reported input/output tokens, estimated and provider-reported context in separate fields, turns, elapsed time, patch size, tool errors, and context reductions are independent telemetry. Synthetic and estimated usage never masquerade as provider usage.
- Context-comparison pass@1 uses the fixed denominator of all 21 scheduled attempts per
  variant. Average input-token cost likewise includes every scheduled attempt and
  requires complete provider-reported usage, including summary calls.
- A forbidden acceptance-path access, dependency installation, or network command is a
  policy violation. Verification is skipped and the attempt is recorded as
  `agent_error`.
- Local shell commands, scripts, command chaining, and Python one-liners are allowed so
  the Agent can inspect and verify its implementation normally. Before subprocess
  launch, the runner still rejects commands that reference acceptance material, use
  recognized network clients or network-capable Git operations, or install/synchronize
  dependencies. Workspace edits also remain available through PaiCLI's file tools.
- Results record suite, runtime, configuration, and environment identities. Replicates require all four identities to match; controlled comparisons vary one declared dimension while holding the others fixed.
- Generated files live under ignored `artifacts/`. `results.json` is the machine-readable source of truth and `report.md` is derived from it. Each attempt also records metadata, patch, final response, redacted/truncated events, and verifier output.
- Writes are atomic. Workspaces are removed by default and may be retained explicitly for debugging. API keys, raw HTTP requests, and private reasoning are never stored. If the Agent writes a configured credential or a recognized credential pattern into its final tree, the attempt becomes `agent_error`, verification is skipped, and the unsafe patch artifact is omitted.

## Interfaces and exit codes

- Package interfaces load/validate a suite and run it with either a production live client factory or a deterministic scripted client factory used at the external model boundary.
- `scripts/evaluate_local_smoke.py` is the repository entry point; PaiCLI's installed CLI is unchanged.
- Exit `0` means every scheduled attempt passed; `1` means argument, setup, or benchmark infrastructure failure; `2` means the run completed with a failed or Agent-error attempt; `130` means user interruption.
- Live runs without `--allow-unsandboxed` fail before any model call. Dirty PaiCLI runtimes are allowed and visibly fingerprinted; `--require-clean-runtime` rejects them.

## Formal comparison

Run from the repository root in the current configured PaiCLI environment:

```powershell
python scripts/evaluate_local_smoke.py `
  --manifest benchmarks/local-smoke-v1/tasks.json `
  --output-dir artifacts/local-smoke-context/formal-001 `
  --compare-contexts `
  --context-profile benchmarks/local-smoke-v1/profiles/stress-16k-v1.json `
  --repetitions 3 `
  --allow-unsandboxed `
  --require-clean-runtime
```

The formal claim is eligible only when all of these conditions hold:

- the runtime is clean and the configured endpoint is
  `qwen/qwen3.7-plus`;
- both variants contain exactly 21 terminal attempts;
- every attempt has provider-reported input usage and no benchmark
  infrastructure failure;
- no attempt records an acceptance-access, network, or dependency-install policy
  violation;
- optimized pass@1 is strictly higher and average provider input tokens are
  strictly lower than full history;
- at least four of five pressure tasks record reduction, and at least two of
  three high-pressure tasks record a summary action.

The generated report then emits the descriptive statement:

> 在固定 local-smoke-v2 任务套件上，将 PaiCLI pass@1 从 xx% 提升至 xx%，平均 token 消耗降低 xx%。

Calibration, scripted-client runs, incomplete runs, and runs that miss any gate may be
reported diagnostically but cannot use this statement as an experiment conclusion.

## Automated verification

CI uses deterministic scripted model clients while retaining the production Agent, real tools, Git patching, verifier, and artifact paths. Tests cover strict manifests, traversal rejection, immutable fixtures, fresh repetitions, acceptance integrity, committed and untracked patch capture, continuation after errors, identities, redaction, workspace cleanup, and exit codes. Ordinary tests require no API key and make no live model calls.
