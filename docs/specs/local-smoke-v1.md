# Local Smoke v1 Coding Evaluation

PaiCLI will add a self-contained seven-task local coding benchmark that exercises the production Agent path. It is an end-to-end integration smoke suite, not a broad coding-capability leaderboard.

## Suite

- `benchmarks/local-smoke-v1/tasks.json` is a strict JSON manifest with `schema_version: 1`, a versioned suite ID, one fixed pytest verifier definition, and seven uniquely identified tasks.
- Five tasks are adapted copies of PaiCLI's long-session scenarios. `string-normalize` and `invoice-totals` are adapted from FirstCoder revision `0067930`; the source projects and their original fixtures remain unchanged.
- Each task references a normal fixture directory and a separate acceptance directory beneath the suite root. Paths must be safe relative paths, exist, remain inside the suite, and not overlap.
- Suite identity is a deterministic fingerprint over the normalized manifest plus every referenced fixture and acceptance file. A semantic task change requires a new suite version.

## Execution

- The task prompt is sent verbatim through PaiCLI's production `QueryEngine`/`Agent` path. No benchmark-only prompt wrapper is added.
- Every attempt starts from a fresh copied fixture, initialized as a Git repository with a recorded base commit and a fresh Agent session.
- The baseline tool profile includes workspace file read/write/list/search and shell tools, while excluding web, browser, MCP, skill, memory, and snapshot-restoration tools.
- Benchmark configuration uses `temperature=0`, `hitl_mode=never`, and the production Agent limits: 20 turns, 40 tool calls, 600 seconds, and 100000 tokens. Tasks cannot override these limits.
- Live execution requires explicit `allow_unsandboxed`; artifacts record `filesystem_isolation=false`, `network_isolation=false`, and whether the risk was acknowledged. This suite is not safe for untrusted models or tasks.
- Attempts run serially. Repetitions default to one; formal comparisons should use three. Every repetition gets a fresh workspace and client.

## Verification

- The runner freezes every fixture and acceptance file while loading and fingerprinting the suite, before any Agent starts. All repetitions materialize from those frozen bytes. This guarantees acceptance integrity, not confidentiality from arbitrary unsandboxed shell access.
- The benchmark patch is the net difference from the recorded base tree to the final file tree, including committed, staged, unstaged, deleted, and untracked changes.
- Verification creates a fresh copy of the fixture, applies the benchmark patch, overlays the preloaded acceptance files, then runs `[sys.executable, "-m", "pytest", "-q"]` with `shell=False` and a 120-second timeout.
- No fixture dependency installation occurs. Acceptance tests use only Python's standard library and pytest.

## Results and artifacts

- Execution status is `completed`, `agent_error`, or `benchmark_error`; verification status is `passed`, `failed`, or `not_run`. A task error does not stop later attempts.
- Correctness is the primary result. Provider-reported input/output tokens, estimated and provider-reported context in separate fields, turns, elapsed time, patch size, tool errors, and context reductions are independent telemetry. Synthetic and estimated usage never masquerade as provider usage.
- Results record suite, runtime, configuration, and environment identities. Replicates require all four identities to match; controlled comparisons vary one declared dimension while holding the others fixed.
- Generated files live under ignored `artifacts/`. `results.json` is the machine-readable source of truth and `report.md` is derived from it. Each attempt also records metadata, patch, final response, redacted/truncated events, and verifier output.
- Writes are atomic. Workspaces are removed by default and may be retained explicitly for debugging. API keys, raw HTTP requests, and private reasoning are never stored. If the Agent writes a configured credential or a recognized credential pattern into its final tree, the attempt becomes `agent_error`, verification is skipped, and the unsafe patch artifact is omitted.

## Interfaces and exit codes

- Package interfaces load/validate a suite and run it with either a production live client factory or a deterministic scripted client factory used at the external model boundary.
- `scripts/evaluate_local_smoke.py` is the repository entry point; PaiCLI's installed CLI is unchanged.
- Exit `0` means every scheduled attempt passed; `1` means argument, setup, or benchmark infrastructure failure; `2` means the run completed with a failed or Agent-error attempt; `130` means user interruption.
- Live runs without `--allow-unsandboxed` fail before any model call. Dirty PaiCLI runtimes are allowed and visibly fingerprinted; `--require-clean-runtime` rejects them.

## Automated verification

CI uses deterministic scripted model clients while retaining the production Agent, real tools, Git patching, verifier, and artifact paths. Tests cover strict manifests, traversal rejection, immutable fixtures, fresh repetitions, acceptance integrity, committed and untracked patch capture, continuation after errors, identities, redaction, workspace cleanup, and exit codes. Ordinary tests require no API key and make no live model calls.
