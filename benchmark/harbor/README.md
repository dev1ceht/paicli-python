# Harbor Evaluation

PaiCLI integrates with Harbor as an installed agent. Harbor owns task selection,
Linux container lifecycle, verifier execution, rewards, and result storage. The
adapter runs the normal production PaiCLI agent, prompt assembly, context
management, and complete built-in tool registry inside each task container.

The adapter is dataset-agnostic. It does not inspect verifier files, apply or
collect patches, reset Git, aggregate scores, or upload results.

## Installation

Install Harbor into a dedicated Python 3.12 environment. This keeps Harbor's
evaluation dependencies separate from PaiCLI's production environment. The
project does not pin or add Harbor to PaiCLI's production dependencies:

```powershell
uv venv .harbor-venv --python 3.12
uv pip install --python .\.harbor-venv\Scripts\python.exe harbor
.\.harbor-venv\Scripts\harbor.exe --version
docker version
```

Docker Desktop must be running in Linux containers mode. Run Harbor from the
repository root and expose that checkout to Python so Harbor can import
`benchmark.harbor.paicli_agent:PaiCliHarborAgent`:

```powershell
$env:PYTHONPATH = (Get-Location).Path
$env:PYTHONUTF8 = "1"
```

## Model Configuration

Benchmark mode ignores host and project PaiCLI configuration files and the
project `.env`. Configure the model only through variables explicitly passed to
the task container:

```powershell
$env:PAICLI_PROVIDER = "openai-compatible"
$env:PAICLI_MODEL = "MODEL"
$env:PAICLI_BASE_URL = "https://provider.example/v1"
$env:PAICLI_API_KEY = "..."
```

Harbor's `-m` value is result metadata. The `PAICLI_*` values configure the
actual PaiCLI process.

## Hello World Smoke Test

Run one isolated trial. A successful acceptance run ends with verifier reward
`1`:

```powershell
.\.harbor-venv\Scripts\harbor.exe run `
  -d harbor/hello-world `
  -a benchmark.harbor.paicli_agent:PaiCliHarborAgent `
  -m "$env:PAICLI_PROVIDER/$env:PAICLI_MODEL" `
  -n 1 -k 1 `
  --agent-setup-timeout-multiplier 3 `
  --ae "PAICLI_PROVIDER=$env:PAICLI_PROVIDER" `
  --ae "PAICLI_MODEL=$env:PAICLI_MODEL" `
  --ae "PAICLI_BASE_URL=$env:PAICLI_BASE_URL" `
  --ae "PAICLI_API_KEY=$env:PAICLI_API_KEY" `
  -o benchmark/runs/harbor/hello-world `
  -y
```

The setup timeout multiplier affects only agent installation. PaiCLI's runtime
budgets remain `100` turns, `200` tool calls, `1800` seconds, and `1,000,000`
total tokens unless overridden with `--ak`, for example
`--ak max_elapsed_seconds=2400`.

## Terminal Bench Trial

Inspect the published dataset and select one task before running the broader
smoke test:

```powershell
.\.harbor-venv\Scripts\harbor.exe dataset download terminal-bench/terminal-bench-2-1 --cache
```

Then run exactly one selected task:

```powershell
.\.harbor-venv\Scripts\harbor.exe run `
  -d terminal-bench/terminal-bench-2-1 `
  -i TASK_NAME `
  -a benchmark.harbor.paicli_agent:PaiCliHarborAgent `
  -m "$env:PAICLI_PROVIDER/$env:PAICLI_MODEL" `
  -n 1 -k 1 `
  --agent-setup-timeout-multiplier 3 `
  --ae "PAICLI_PROVIDER=$env:PAICLI_PROVIDER" `
  --ae "PAICLI_MODEL=$env:PAICLI_MODEL" `
  --ae "PAICLI_BASE_URL=$env:PAICLI_BASE_URL" `
  --ae "PAICLI_API_KEY=$env:PAICLI_API_KEY" `
  -o benchmark/runs/harbor/terminal-bench-smoke `
  -y
```

Acceptance requires a completed trial with a valid verifier result; container
startup or dataset download alone is not sufficient.

## Aider Polyglot Java verifier

The Java tasks in the local Aider Polyglot dataset use a Gradle Wrapper whose
default download timeout is only ten seconds. The checked-in override at
`benchmark/harbor/aider-polyglot/gradle-wrapper.properties` raises it to five
minutes. Mount it over the task's wrapper properties when running a Java task.
The mount must remain writable because the dataset verifier appends its own
Gradle daemon setting before invoking `./gradlew`:

```powershell
$gradleOverride = (Resolve-Path .\benchmark\harbor\aider-polyglot\gradle-wrapper.properties).Path -replace '\\', '/'
$gradleMount = ConvertTo-Json -InputObject @([ordered]@{
  type = "bind"
  source = $gradleOverride
  target = "/app/gradle/wrapper/gradle-wrapper.properties"
}) -Compress
$gradleMount = $gradleMount -replace '"', '\\"'

.\.harbor-venv\Scripts\harbor.exe run `
  -d aider/aider-polyglot `
  -i aider/polyglot_java_affine-cipher `
  -a benchmark.harbor.paicli_agent:PaiCliHarborAgent `
  -m "$env:PAICLI_PROVIDER/$env:PAICLI_MODEL" `
  --mounts $gradleMount `
  ...
```

## Source and Isolation

By default each trial stages the current checkout's `pyproject.toml`,
`README.md`, and `src/paicli/`, including uncommitted source changes. `.git`,
`.env`, tests, virtual environments, and host configuration are excluded. Pass
`--ak package=PACKAGE_OR_GIT_URL` only when an explicit published-source
fallback is intended. There is no silent package fallback.

Each trial receives a fresh container, process, virtual environment, and clean
HOME. No persistent PaiCLI session ID is added. Project instructions and skills
inside the task worktree remain available; host-global skills do not.

MCP is disabled by default. An explicit Harbor-owned config can be supplied
with `--ak mcp_config_path=PATH`; credential values in that file must use
`${ENV_NAME}` references.

## Results and Credentials

Harbor stores trial status, agent logs, verifier logs, reward, and timing under
the selected output directory. PaiCLI additionally writes `events.jsonl`,
`paicli.txt`, `final.txt`, and `runtime.json` under `/logs/agent` in the trial.
The runtime metadata includes source, configuration, and installed-dependency
fingerprints.

Logs apply best-effort secret redaction. Provider and explicitly configured MCP
secrets remain environment variables in the task and are therefore visible to
child shell commands. This is diagnostic redaction, not credential isolation.
Do not add `--upload` unless publishing results is explicitly intended.
