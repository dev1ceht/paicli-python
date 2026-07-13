# Project-configurable agent run limits

PaiCLI will ship conservative defaults for ReAct turn count, tool-call count, elapsed time, and token consumption, while allowing a project to override them in `.paicli/config.json`. This keeps normal use protected without forcing a single budget on every model or repository.
