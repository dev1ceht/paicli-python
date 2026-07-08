# PaiCLI Python

PaiCLI Python is a terminal AI agent CLI for real project work. It brings an
interactive coding-agent experience to Python, with ReAct-style tool use, MCP
integration, local memory, project snapshots, web tools, image input, and a
Runtime API for agent threads and background tasks.

This repository is the Python implementation of PaiCLI. It is built as a real
CLI product rather than a toy demo: the core paths are covered by tests, local
smoke checks, and real terminal runs.

## Features

- Interactive terminal agent with a compact Rich/prompt-toolkit UI
- Single prompt mode for scripts and shell pipelines
- OpenAI-compatible streaming LLM client, with DeepSeek defaults
- Provider-specific API key support such as `DEEPSEEK_API_KEY`
- ReAct loop with thinking, tool call, tool result, final output, and usage events
- Built-in file, shell, grep, glob, memory, web search, web fetch, and code search tools
- Human-in-the-loop approval, command/path guards, and JSONL audit logs
- MCP client for stdio and Streamable HTTP servers
- Chrome DevTools MCP config helper
- PaiCLI as an MCP server over stdio or HTTP
- Runtime API for threads, turns, event logs, and durable background tasks
- SQLite-backed memory and code index
- Pre-turn and post-turn snapshots with restore support
- Local and remote image input with model-capability fallback

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)
- Optional: `rg` for faster local search
- Optional for Chrome DevTools MCP: Node.js 20.19.0 LTS or newer, npm/npx, and Chrome

## Quick Start

```bash
git clone https://github.com/itwanger/PaiCLI-Python.git
cd PaiCLI-Python
uv sync --extra dev
uv run paicli --help
```

Run the interactive CLI:

```bash
uv run paicli
```

Run a single prompt:

```bash
uv run paicli -p "Summarize this project"
```

Inspect the local environment:

```bash
uv run paicli doctor --cwd .
```

## Configuration

PaiCLI reads configuration in this order:

1. Built-in defaults
2. `~/.paicli/config.json`
3. Project `.paicli/config.json`
4. Project `.env`
5. CLI flags
6. Current process environment variables

You can configure DeepSeek in `.env`, the same way many Java projects do:

```dotenv
PAICLI_PROVIDER=deepseek
PAICLI_MODEL=deepseek-v4-flash
DEEPSEEK_API_KEY=your_key_here
```

You can also use the generic PaiCLI key:

```dotenv
PAICLI_PROVIDER=deepseek
PAICLI_MODEL=deepseek-v4-flash
PAICLI_API_KEY=your_key_here
```

Provider-specific environment keys currently include:

- `DEEPSEEK_API_KEY`
- `GLM_API_KEY`
- `STEP_API_KEY`
- `KIMI_API_KEY`

Override provider/model from the command line:

```bash
uv run paicli --provider deepseek --model deepseek-v4-flash
```

Use an OpenAI-compatible local endpoint:

```bash
PAICLI_PROVIDER=openai-compatible \
PAICLI_BASE_URL=http://127.0.0.1:11434/v1 \
PAICLI_MODEL=qwen2.5-coder \
uv run paicli -p "Explain this repository"
```

## Interactive Commands

Inside `uv run paicli`, these slash commands are available:

```text
/help
/exit
/clear
/context
/memory
/memory search <query>
/memory clear
/save <fact>
/config
/tools
/hitl on|off|always|auto|never
/policy
/audit [N]
/index [path]
/search <query>
/plan <task>
/team <task>
/model
/skill
/skill show <name>
/mcp
/task
/task add <task>
/task cancel <task_id>
/task log <task_id>
/snapshot
/snapshot clean
/restore <snapshot-id-or-index>
```

## Built-in Tools

PaiCLI ships with local and web tools that can be called by the agent:

- `read_file`
- `write_file`
- `list_dir`
- `glob` / `glob_files`
- `grep` / `grep_code`
- `bash` / `execute_command`
- `web_search`
- `web_fetch`
- `save_memory`
- `load_skill`
- `search_code`
- `revert_turn`

Dangerous actions such as file writes, command execution, remote MCP writes, and
snapshot restores are routed through policy checks, HITL approval, and audit logs.

## Web Tools

`web_search` uses DuckDuckGo HTML search and returns titles, URLs, and snippets.

`web_fetch` fetches public HTTP/HTTPS pages and performs basic text extraction.
It rejects `file://`, loopback, private network, and internal addresses to reduce
SSRF risk.

For logged-in pages, browser state, or JavaScript-heavy workflows, use Chrome
DevTools MCP instead.

## MCP

PaiCLI can connect to MCP servers and dynamically expose remote tools under:

```text
mcp__<server-name>__<tool-name>
```

Initialize project-level Chrome DevTools MCP config:

```bash
uv run paicli mcp init-chrome --scope project
```

This writes `.paicli/mcp.json` similar to:

```json
{
  "mcpServers": {
    "chrome-devtools": {
      "type": "stdio",
      "command": "npx",
      "args": [
        "-y",
        "chrome-devtools-mcp@latest",
        "--no-usage-statistics"
      ]
    }
  }
}
```

Connect to an existing remote-debugging Chrome:

```bash
uv run paicli mcp init-chrome \
  --scope project \
  --browser-url http://127.0.0.1:9222
```

List configured MCP servers:

```bash
uv run paicli mcp list
```

Run PaiCLI itself as an MCP server:

```bash
uv run paicli mcp serve --transport stdio
uv run paicli mcp serve --transport http --port 3000
```

HTTP smoke test:

```bash
curl -sS -X POST http://127.0.0.1:3000 \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

Chrome DevTools MCP can expose browser pages and DevTools state to the agent.
Do not connect it to sensitive personal accounts or production systems unless
you are comfortable granting that access.

## Runtime API

PaiCLI includes a lightweight Runtime API for external clients that need thread,
turn, event, and task primitives.

Start the server:

```bash
PAICLI_RUNTIME_API_KEY=dev-key \
uv run paicli serve --http --port 8080
```

Create a thread:

```bash
curl -sS -X POST http://127.0.0.1:8080/v1/threads \
  -H 'x-api-key: dev-key'
```

Send a turn:

```bash
curl -sS -X POST http://127.0.0.1:8080/v1/threads/<thread_id>/turns \
  -H 'content-type: application/json' \
  -H 'x-api-key: dev-key' \
  -d '{"message":"Summarize this project"}'
```

Read events:

```bash
curl -sS http://127.0.0.1:8080/v1/threads/<thread_id>/events \
  -H 'x-api-key: dev-key'
```

Create and inspect a background task:

```bash
curl -sS -X POST http://127.0.0.1:8080/v1/tasks \
  -H 'content-type: application/json' \
  -H 'x-api-key: dev-key' \
  -d '{"message":"Summarize this repository in the background"}'

curl -sS http://127.0.0.1:8080/v1/tasks \
  -H 'x-api-key: dev-key'
```

## Image Input

PaiCLI supports image references in prompts:

```text
Analyze this screenshot @image:./screenshots/page.png
```

Absolute paths and remote images are also supported:

```text
Explain this diagram @image:/Users/me/Desktop/diagram.png
Review this image @image:https://example.com/image.png
```

Local images are resized, compressed, normalized onto a white background when
needed, and converted to data URLs. If the selected provider/model does not
support multimodal input, PaiCLI falls back to text metadata instead of sending
an unsupported image payload.

## Snapshots

Each agent run creates best-effort project snapshots:

- `pre-turn`
- `post-turn`

Snapshots are stored under `~/.paicli/snapshots/` and do not write into the
project `.git` directory.

Inside the REPL:

```text
/snapshot
/restore 1
/snapshot clean
```

## SDK

```python
from paicli.sdk import create_default_engine

engine = create_default_engine(cwd=".")
result = engine.ask_complete("Explain this project")
print(result.text)
```

## Development

Install dev dependencies:

```bash
uv sync --extra dev
```

Run checks:

```bash
uv run python -m ruff check .
uv run python -m ruff format --check .
uv run python -m pytest
uv build
```

Useful smoke commands:

```bash
uv run paicli --version
uv run paicli --help
uv run paicli doctor --cwd .
uv run paicli --plain -p hello
```

## Parity Notes

The Python version covers the public/open Agent CLI surfaces from the Java and
TypeScript versions, including CLI mode, REPL commands, tool use, MCP, runtime
API, memory, snapshots, web tools, and image input.

The Java implementation also has a private WeChat iLink channel. This Python
repository does not ship that private channel because it requires account,
scan-login, and protocol credentials that should not be faked.

See [docs/parity.md](docs/parity.md) for a more detailed implementation map.

## License

MIT. See [LICENSE](LICENSE).
