from __future__ import annotations

import asyncio
import glob as glob_module
import os
import re
import signal
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import Any

from paicli.browser import BrowserSession
from paicli.lsp import diagnose_file
from paicli.memory import MemoryManager
from paicli.policy import CommandGuard, PathGuard
from paicli.rag import CodeIndex
from paicli.skill import SkillRegistry
from paicli.snapshot import SnapshotService
from paicli.tools.base import Tool, ToolContext, ToolResult, object_schema
from paicli.web import fetch_url, search_web


def get_builtin_tools() -> list[Tool]:
    tools = [
        Tool(
            name="read_file",
            description="Read a text file from the current workspace.",
            parameters=object_schema(
                {
                    "path": {"type": "string", "description": "Path to read"},
                    "offset": {"type": "number", "description": "Start line, 1-based"},
                    "limit": {"type": "number", "description": "Maximum number of lines"},
                },
                ["path"],
            ),
            required_keys=["path"],
            handler=read_file,
        ),
        Tool(
            name="write_file",
            description="Write a UTF-8 text file inside the current workspace.",
            parameters=object_schema(
                {
                    "path": {"type": "string", "description": "Path to write"},
                    "content": {"type": "string", "description": "File content"},
                    "append": {"type": "boolean", "description": "Append instead of overwrite"},
                },
                ["path", "content"],
            ),
            required_keys=["path", "content"],
            handler=write_file,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="medium",
            requires_approval=True,
        ),
        Tool(
            name="list_dir",
            description="List entries in a directory inside the current workspace.",
            parameters=object_schema(
                {"path": {"type": "string", "description": "Directory path"}},
                ["path"],
            ),
            required_keys=["path"],
            handler=list_dir,
        ),
        Tool(
            name="glob",
            description="Find files by glob pattern inside the current workspace.",
            parameters=object_schema(
                {
                    "pattern": {"type": "string", "description": "Glob pattern"},
                    "limit": {"type": "number", "description": "Maximum results"},
                },
                ["pattern"],
            ),
            required_keys=["pattern"],
            handler=glob_files,
        ),
        Tool(
            name="glob_files",
            description="Alias of glob. Find files by glob pattern inside the current workspace.",
            parameters=object_schema(
                {
                    "pattern": {"type": "string", "description": "Glob pattern"},
                    "limit": {"type": "number", "description": "Maximum results"},
                },
                ["pattern"],
            ),
            required_keys=["pattern"],
            handler=glob_files,
        ),
        Tool(
            name="grep",
            description="Search text in workspace files.",
            parameters=object_schema(
                {
                    "pattern": {"type": "string", "description": "Regex or plain text pattern"},
                    "path": {"type": "string", "description": "Optional path to search"},
                    "regex": {"type": "boolean", "description": "Treat pattern as regex"},
                    "limit": {"type": "number", "description": "Maximum matches"},
                },
                ["pattern"],
            ),
            required_keys=["pattern"],
            handler=grep,
        ),
        Tool(
            name="grep_code",
            description="Alias of grep. Search text in workspace files.",
            parameters=object_schema(
                {
                    "pattern": {"type": "string", "description": "Regex or plain text pattern"},
                    "path": {"type": "string", "description": "Optional path to search"},
                    "regex": {"type": "boolean", "description": "Treat pattern as regex"},
                    "limit": {"type": "number", "description": "Maximum matches"},
                },
                ["pattern"],
            ),
            required_keys=["pattern"],
            handler=grep,
        ),
        Tool(
            name="bash",
            description="Execute a shell command in the current workspace.",
            parameters=object_schema(
                {
                    "command": {"type": "string", "description": "Shell command"},
                    "timeout": {"type": "number", "description": "Timeout seconds"},
                },
                ["command"],
            ),
            required_keys=["command"],
            handler=bash,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="high",
            requires_approval=True,
        ),
        Tool(
            name="execute_command",
            description="Alias of bash. Execute a shell command in the current workspace.",
            parameters=object_schema(
                {
                    "command": {"type": "string", "description": "Shell command"},
                    "timeout": {"type": "number", "description": "Timeout seconds"},
                },
                ["command"],
            ),
            required_keys=["command"],
            handler=bash,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="high",
            requires_approval=True,
        ),
        Tool(
            name="web_search",
            description=(
                "Search the web for current information. Returns titles, URLs, and snippets."
            ),
            parameters=object_schema(
                {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {"type": "number", "description": "Maximum result count"},
                },
                ["query"],
            ),
            required_keys=["query"],
            handler=web_search,
        ),
        Tool(
            name="web_fetch",
            description="Fetch a public HTTP/HTTPS page and return readable text.",
            parameters=object_schema(
                {
                    "url": {"type": "string", "description": "URL to fetch"},
                    "max_length": {"type": "number", "description": "Maximum returned characters"},
                },
                ["url"],
            ),
            required_keys=["url"],
            handler=web_fetch,
        ),
        Tool(
            name="browser_status",
            description="Show current Chrome DevTools MCP browser session mode.",
            parameters=object_schema({}),
            handler=browser_status,
        ),
        Tool(
            name="browser_connect",
            description=(
                "Switch Chrome DevTools MCP to shared mode using --autoConnect or a CDP port."
            ),
            parameters=object_schema(
                {"port": {"type": "number", "description": "Optional local CDP port"}},
            ),
            handler=browser_connect,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="medium",
            requires_approval=True,
        ),
        Tool(
            name="browser_disconnect",
            description="Switch Chrome DevTools MCP back to isolated browser mode.",
            parameters=object_schema({}),
            handler=browser_disconnect,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="medium",
            requires_approval=True,
        ),
        Tool(
            name="browser_tabs",
            description="List tabs from a Chrome DevTools CDP browser URL session.",
            parameters=object_schema({}),
            handler=browser_tabs,
        ),
        Tool(
            name="save_memory",
            description=(
                "Save a stable fact to long-term memory only when the user explicitly "
                "asks to remember it. Use scope=project by default, scope=global for "
                "cross-project preferences."
            ),
            parameters=object_schema(
                {
                    "fact": {"type": "string", "description": "Stable fact to remember"},
                    "content": {
                        "type": "string",
                        "description": "Legacy alias for fact",
                    },
                    "scope": {
                        "type": "string",
                        "description": "project or global; defaults to project",
                    },
                },
                [],
            ),
            handler=save_memory,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="medium",
        ),
        Tool(
            name="load_skill",
            description="Load a named PaiCLI skill manual from user/project skill directories.",
            parameters=object_schema(
                {"name": {"type": "string", "description": "Skill name"}},
                ["name"],
            ),
            required_keys=["name"],
            handler=load_skill,
        ),
        Tool(
            name="search_code",
            description="Search the local code index for semantically relevant lines.",
            parameters=object_schema(
                {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "number", "description": "Maximum matches"},
                },
                ["query"],
            ),
            required_keys=["query"],
            handler=search_code,
        ),
        Tool(
            name="revert_turn",
            description="Restore the workspace to a previous PaiCLI side-history snapshot.",
            parameters=object_schema(
                {"snapshot": {"type": "string", "description": "Snapshot id or 1-based index"}},
                ["snapshot"],
            ),
            required_keys=["snapshot"],
            handler=revert_turn,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="high",
            requires_approval=True,
            mandatory_confirmation=True,
        ),
    ]
    return tools


async def read_file(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    path = _resolve_path(context, str(payload["path"]))
    offset = max(int(payload.get("offset") or 1), 1)
    limit = int(payload.get("limit") or 500)
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    selected = content[offset - 1 : offset - 1 + limit]
    numbered = "\n".join(f"{idx + offset}: {line}" for idx, line in enumerate(selected))
    return ToolResult(numbered, display_summary=f"Read {path.relative_to(context.cwd)}")


async def write_file(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    path = _resolve_path(context, str(payload["path"]))
    content = str(payload["content"])
    if len(content.encode("utf-8")) > 5 * 1024 * 1024:
        return ToolResult("write_file rejected: content exceeds 5MB", is_error=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if payload.get("append") else "w"
    with path.open(mode, encoding="utf-8") as handle:
        handle.write(content)
    rel = path.relative_to(context.cwd)
    diagnostics = diagnose_file(path)
    suffix = ""
    if diagnostics:
        suffix = "\n\nDiagnostics:\n" + "\n".join(diagnostics)
    return ToolResult(f"Wrote {rel}{suffix}", display_summary=f"Wrote {rel}")


async def list_dir(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    path = _resolve_path(context, str(payload["path"]))
    if not path.is_dir():
        return ToolResult(f"Not a directory: {path}", is_error=True)
    rows = []
    for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        marker = "/" if child.is_dir() else ""
        rows.append(f"{child.name}{marker}")
    return ToolResult("\n".join(rows) or "(empty directory)")


async def glob_files(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    root = Path(context.cwd).resolve()
    pattern = str(payload["pattern"])
    if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
        return ToolResult("glob pattern must stay inside workspace", is_error=True)
    limit = int(payload.get("limit") or 100)
    matches = glob_module.glob(str(root / pattern), recursive=True)
    rels = []
    for match in sorted(matches):
        path = Path(match).resolve()
        try:
            rels.append(str(path.relative_to(root)))
        except ValueError:
            continue
        if len(rels) >= limit:
            break
    return ToolResult("\n".join(rels) or "(no matches)")


async def grep(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    root = Path(context.cwd).resolve()
    start = _resolve_path(context, str(payload.get("path") or "."))
    pattern = str(payload["pattern"])
    limit = int(payload.get("limit") or 100)
    use_regex = bool(payload.get("regex", True))
    try:
        compiled = re.compile(pattern) if use_regex else None
    except re.error as exc:
        return ToolResult(f"invalid regex: {exc}", is_error=True)

    matches: list[str] = []
    files = [start] if start.is_file() else [p for p in start.rglob("*") if p.is_file()]
    for file_path in files:
        if _skip_file(file_path):
            continue
        try:
            lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, start=1):
            found = bool(compiled.search(line)) if compiled else pattern in line
            if found:
                matches.append(f"{file_path.relative_to(root)}:{line_number}: {line.strip()}")
                if len(matches) >= limit:
                    return ToolResult("\n".join(matches))
    return ToolResult("\n".join(matches) or "(no matches)")


async def bash(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    command = str(payload["command"])
    CommandGuard(context.config.policy.command_blacklist).validate(command)
    timeout = float(payload.get("timeout") or context.config.tools.timeout)
    process_options: dict[str, Any] = {}
    if os.name == "nt":
        process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        process_options["start_new_session"] = True
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=context.cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
        **process_options,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        await _stop_process_tree(proc)
        return ToolResult(f"Command timed out after {timeout:.0f}s", is_error=True)
    except asyncio.CancelledError:
        if context.cancellation_check and proc.returncode is None:
            await _stop_process_tree(proc)
        raise
    output = (stdout + stderr).decode("utf-8", errors="replace")
    if len(output) > 20_000:
        output = output[:20_000] + "\n... [truncated]"
    return ToolResult(
        output or f"(exit {proc.returncode}, no output)",
        is_error=proc.returncode != 0,
    )


async def _stop_process_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    if os.name == "nt":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(proc.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        except OSError:
            with suppress(ProcessLookupError):
                proc.kill()
    else:
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
    except TimeoutError:
        if os.name != "nt":
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        else:
            with suppress(ProcessLookupError):
                proc.kill()
        with suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=3)


async def web_search(payload: dict[str, Any], _context: ToolContext) -> ToolResult:
    max_results = int(payload.get("max_results") or payload.get("maxResults") or 5)
    try:
        results = await search_web(str(payload["query"]), max_results=max_results)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(f"Search error: {exc}", is_error=True)
    if not results:
        return ToolResult(f'No search results found for "{payload["query"]}".')
    content = "\n\n".join(
        f"{index}. {result.title}\n{result.url}\n{result.snippet}"
        for index, result in enumerate(results, start=1)
    )
    return ToolResult(content, display_summary=f"Search: {len(results)} results")


async def web_fetch(payload: dict[str, Any], _context: ToolContext) -> ToolResult:
    max_length = int(payload.get("max_length") or payload.get("maxLength") or 10_000)
    try:
        content = await fetch_url(str(payload["url"]), max_length=max_length)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(f"Fetch error: {exc}", is_error=True)
    return ToolResult(content, display_summary=f"Fetched {payload['url']}")


async def browser_status(_payload: dict[str, Any], context: ToolContext) -> ToolResult:
    state = BrowserSession(context.cwd).status()
    suffix = f" ({state.browser_url})" if state.browser_url else ""
    return ToolResult(f"browser mode: {state.mode}{suffix}")


async def browser_connect(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    raw_port = payload.get("port")
    port = int(raw_port) if raw_port not in (None, "") else None
    state = BrowserSession(context.cwd).connect(port=port)
    suffix = f" at {state.browser_url}" if state.browser_url else " with --autoConnect"
    return ToolResult(f"browser mode: {state.mode}{suffix}")


async def browser_disconnect(_payload: dict[str, Any], context: ToolContext) -> ToolResult:
    state = BrowserSession(context.cwd).disconnect()
    return ToolResult(f"browser mode: {state.mode}")


async def browser_tabs(_payload: dict[str, Any], context: ToolContext) -> ToolResult:
    tabs = BrowserSession(context.cwd).tabs()
    if not tabs:
        return ToolResult("No browser tabs available. Use /browser connect <port> for CDP tabs.")
    return ToolResult("\n".join(f"{tab.id}\t{tab.title}\t{tab.url}" for tab in tabs))


async def save_memory(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    if not context.config.features.memory or not context.config.memory.long_term_enabled:
        return ToolResult("Long-term memory is disabled.", is_error=True)
    fact = str(payload.get("fact") or payload.get("content") or "").strip()
    if not fact:
        return ToolResult("保存长期记忆失败: fact 不能为空", is_error=True)
    scope = "global" if str(payload.get("scope") or "").lower() == "global" else "project"
    manager = MemoryManager(context.config.memory.long_term_path, project_path=context.cwd)
    result = await manager.save_with_classification(fact, scope=scope, llm_client=context.llm_client)
    if result.status == "pending":
        return ToolResult(f"Created pending memory change: {result.change_id}")
    if result.status == "duplicate":
        return ToolResult(f"Memory already exists: {result.memory_id}")
    return ToolResult(f"已保存到长期记忆({scope}): {fact}")


async def load_skill(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    skill = SkillRegistry(context.cwd).load(str(payload["name"]))
    if not skill:
        return ToolResult(f'Skill "{payload["name"]}" not found.', is_error=True)
    content = skill.content
    if len(content) > 12_000:
        content = content[:12_000] + "\n... [truncated]"
    return ToolResult(content, display_summary=f"Loaded skill {skill.name}")


async def search_code(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    index = CodeIndex(context.cwd)
    results = index.search(str(payload["query"]), limit=int(payload.get("limit") or 20))
    if not results:
        return ToolResult("(no indexed matches; run /index first)")
    return ToolResult("\n".join(f"{item.path}:{item.line}: {item.snippet}" for item in results))


async def revert_turn(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    record = SnapshotService(context.cwd).restore(str(payload["snapshot"]))
    return ToolResult(f"Restored snapshot {record.id}")


def _resolve_path(context: ToolContext, value: str) -> Path:
    if context.config.policy.path_guard_enabled:
        return PathGuard(context.cwd).validate(value)
    path = Path(value)
    return path if path.is_absolute() else Path(context.cwd).resolve() / path


def _skip_file(path: Path) -> bool:
    skip_dirs = {".git", ".venv", "node_modules", "dist", "build", "target"}
    if any(part in skip_dirs for part in path.parts):
        return True
    return path.stat().st_size > 1_000_000
