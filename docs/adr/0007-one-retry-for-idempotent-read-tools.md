# One retry for idempotent read tools

PaiCLI will not automatically retry tools with side effects, commands, or approval denials. It may retry a marked read-only and idempotent tool once for a transient failure, while returning all other failures to the model for a deliberate next action.
