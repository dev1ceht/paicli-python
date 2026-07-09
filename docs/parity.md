# PaiCLI Python Parity

This file tracks the Python port against the existing Java and TypeScript implementations.

## Implemented

- CLI:
  - `paicli`
  - `paicli -p`
  - `--provider`
  - `--model`
  - `--plain`
  - `--cwd`
  - `paicli doctor`
  - `paicli serve --http --port <port>`
- REPL:
  - `/help`
  - `/clear`
  - `/context`
  - `/memory`
  - `/save`
  - `/config`
  - `/tools`
  - `/hitl`
  - `/policy`
  - `/audit`
  - `/index`
  - `/search`
  - `/plan`
  - `/team`
  - `/task`
  - `/snapshot`
  - `/restore`
  - `/skill`
  - `/mcp`
  - `/exit`
- Agent:
  - OpenAI-compatible streaming LLM client
  - DeepSeek default
  - ReAct loop with text/thinking/tool-call/tool-result/done events
  - Plan-and-Execute mode with JSON planning, dependency-aware task execution, and plan events
  - SDK entrypoint
  - pre/post side-history snapshots around Agent runs
- Configuration:
  - defaults
  - user config
  - project config
  - project `.env`
  - CLI overrides
  - process env
  - provider-specific keys such as `DEEPSEEK_API_KEY`, `GLM_API_KEY`, `STEP_API_KEY`, `KIMI_API_KEY`
- Tools:
  - `read_file`
  - `write_file`
  - `list_dir`
  - `glob` / `glob_files`
  - `grep` / `grep_code`
  - `bash` / `execute_command`
  - `web_search`
  - `web_fetch`
  - `browser_status`
  - `browser_connect`
  - `browser_disconnect`
  - `browser_tabs`
  - `save_memory`
  - `load_skill`
  - `search_code`
  - `revert_turn`
- Safety:
  - PathGuard
  - CommandGuard
  - HITL approval
  - JSONL AuditLog
- Memory:
  - SQLite long-term memory
  - project memory files `PAI.md`, `.paicli/PAI.md`, local variants
- Skills:
  - user/project `.paicli/skills/*/SKILL.md`
  - `load_skill`
- RAG:
  - SQLite local code index
  - `/index`
  - `/search`
  - `search_code`
- MCP:
  - official MCP Python SDK client
  - stdio MCP server connection
  - Streamable HTTP MCP server connection
  - dynamic `mcp__server__tool` registration
  - virtual resource tools
  - virtual prompt tools
  - `paicli mcp init-chrome`
  - `paicli mcp list`
  - REPL lifecycle commands: `/mcp`, `/mcp restart <name>`, `/mcp logs <name>`,
    `/mcp disable <name>`, `/mcp enable <name>`, `/mcp resources <name>`,
    `/mcp prompts <name>`
  - per-server stderr logs under `.paicli/mcp-logs/`
  - PaiCLI MCP server over stdio/http for built-in tools
- Chrome DevTools MCP:
  - project/user config writer for `npx chrome-devtools-mcp@latest`
  - `--browser-url`
  - REPL browser commands: `/browser status`, `/browser connect [port]`,
    `/browser disconnect`, `/browser tabs`
  - shared CDP mode via `--autoConnect` or `--browser-url=http://127.0.0.1:<port>`
  - isolated mode restoration via `--isolated=true`
  - `--headless`
  - `--slim`
  - usage-statistics opt-out flag by default
- Runtime:
  - API key requirement
  - `POST /v1/threads`
  - `POST /v1/threads/{id}/turns`
  - `GET /v1/threads/{id}/events`
  - `POST /v1/tasks`
  - `GET /v1/tasks`
  - `GET /v1/tasks/{id}`
  - `POST /v1/tasks/{id}/cancel`
  - SQLite durable task queue
- Snapshot:
  - `pre-turn` / `post-turn`
  - `/snapshot`
  - `/restore`
  - `revert_turn`
- Image input:
  - `@image:path`
  - `@image:file:///path`
  - `@image:https://...`
  - local image resize/compress
  - transparent PNG white background handling
  - provider/model capability fallback
- Diagnostics:
  - Python syntax diagnostics after `write_file`

## Live Dependencies

These features need external credentials or platform state for live verification:

- Real LLM calls need API keys.
- Chrome DevTools MCP needs Node.js LTS, npm/npx, and Chrome.
- Runtime API turn execution needs a working LLM key.
- WeChat iLink needs private iLink credentials and scan-login state.

## Known Remaining Java-Only Area

The Java implementation has a WeChat iLink channel. Python does not ship that private channel yet because it requires iLink account credentials and protocol details that should not be faked. All public/open protocol surfaces from the TypeScript baseline and the main Java Agent CLI surfaces have corresponding Python implementations.

## Verification

```bash
uv run --extra dev ruff check .
uv run --extra dev ruff format --check .
uv run --extra dev pytest
uv build
uv run paicli --help
uv run paicli doctor --cwd .
uv run paicli mcp serve --transport http --port 3999
```
