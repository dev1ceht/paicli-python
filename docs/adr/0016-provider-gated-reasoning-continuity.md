# Provider-gated reasoning continuity

Agent messages will retain model reasoning as an internal optional field. It will be serialized as `reasoning_content` only for providers that explicitly require it; DeepSeek enables the capability and other OpenAI-compatible providers default to disabled. This restores DeepSeek multi-turn tool continuity without sending unsupported non-standard fields to other providers.
