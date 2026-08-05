# Project-configurable agent run limits

PaiCLI will ship conservative defaults for ReAct turn count, tool-call count, and elapsed time, while allowing a project to override them in `.paicli/config.json`. Per-request model output limits remain part of the model configuration, and cumulative token usage is recorded for telemetry rather than enforced as a run cutoff. This keeps normal use protected without forcing a single budget on every model or repository.
