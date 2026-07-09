from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from pydantic import AnyUrl

from paicli.mcp.config import McpServerSpec, load_mcp_server_specs
from paicli.tools.base import Tool, ToolContext, ToolResult, object_schema


class McpClientManager:
    def __init__(self, project_root: str | Path):
        self.project_root = str(Path(project_root).resolve())
        self.project_config_path = Path(self.project_root) / ".paicli" / "mcp.json"
        self.log_dir = Path(self.project_root) / ".paicli" / "mcp-logs"
        self.specs = load_mcp_server_specs(self.project_root)
        self.last_errors: dict[str, str] = {}

    async def load_tools(self) -> list[Tool]:
        tools: list[Tool] = []
        self.last_errors.clear()
        for spec in self.specs.values():
            if not spec.enabled:
                continue
            try:
                tools.extend(await self.load_server_tools(spec.name))
            except Exception as exc:  # noqa: BLE001 - keep broken MCP servers isolated
                self.last_errors[spec.name] = str(exc)
        return tools

    async def load_server_tools(self, name: str) -> list[Tool]:
        spec = self.specs.get(name)
        if not spec or not spec.enabled:
            return []
        tools = await self._tools_for_server(spec)
        tools.extend(self._virtual_resource_tools(spec))
        tools.extend(self._virtual_prompt_tools(spec))
        return tools

    def status(self) -> list[dict[str, Any]]:
        rows = []
        for spec in self.specs.values():
            if not spec.enabled:
                state = "disabled"
            elif spec.name in self.last_errors:
                state = "error"
            else:
                state = "configured"
            rows.append(
                {
                    "name": spec.name,
                    "type": spec.type,
                    "enabled": spec.enabled,
                    "status": state,
                    "target": spec.url or f"{spec.command or ''} {' '.join(spec.args)}".strip(),
                    "error": self.last_errors.get(spec.name, ""),
                }
            )
        return rows

    def enable(self, name: str) -> bool:
        return self._set_enabled(name, True)

    def disable(self, name: str) -> bool:
        return self._set_enabled(name, False)

    async def restart(self, name: str) -> int:
        spec = self.specs.get(name)
        if not spec or not spec.enabled:
            return 0
        self.last_errors.pop(name, None)
        try:
            return len(await self._tools_for_server(spec))
        except Exception as exc:  # noqa: BLE001
            self.last_errors[name] = str(exc)
            return 0

    def logs(self, name: str, limit: int = 200) -> str:
        log_path = self._log_path(name)
        if not log_path.exists():
            error = self.last_errors.get(name)
            return error or "(no logs)"
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-limit:]) or "(no logs)"

    async def list_server_tools(self, spec: McpServerSpec) -> list[Any]:
        async with self._session(spec) as session:
            result = await session.list_tools()
            return list(result.tools)

    async def call_server_tool(
        self,
        spec: McpServerSpec,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        async with self._session(spec) as session:
            result = await session.call_tool(
                tool_name,
                arguments,
                read_timeout_seconds=timedelta(seconds=spec.timeout),
            )
        return ToolResult(content=_content_to_text(result.content), is_error=bool(result.isError))

    async def list_resources(self, spec: McpServerSpec) -> ToolResult:
        async with self._session(spec) as session:
            result = await session.list_resources()
        lines = [
            f"{resource.uri} {resource.name or ''} {resource.description or ''}".strip()
            for resource in result.resources
        ]
        return ToolResult("\n".join(lines) or "(no resources)")

    async def read_resource(self, spec: McpServerSpec, uri: str) -> ToolResult:
        async with self._session(spec) as session:
            result = await session.read_resource(AnyUrl(uri))
        return ToolResult(_content_to_text(result.contents))

    async def list_prompts(self, spec: McpServerSpec) -> ToolResult:
        async with self._session(spec) as session:
            result = await session.list_prompts()
        lines = [f"{prompt.name} {prompt.description or ''}".strip() for prompt in result.prompts]
        return ToolResult("\n".join(lines) or "(no prompts)")

    async def get_prompt(
        self,
        spec: McpServerSpec,
        name: str,
        arguments: dict[str, str] | None = None,
    ) -> ToolResult:
        async with self._session(spec) as session:
            result = await session.get_prompt(name, arguments or {})
        return ToolResult(_content_to_text(result.messages))

    async def _tools_for_server(self, spec: McpServerSpec) -> list[Tool]:
        remote_tools = await self.list_server_tools(spec)
        wrapped: list[Tool] = []
        for remote_tool in remote_tools:
            tool_name = str(remote_tool.name)
            local_name = f"mcp__{spec.name}__{tool_name}"
            schema = remote_tool.inputSchema or object_schema({})
            annotations = getattr(remote_tool, "annotations", None)
            read_only = bool(getattr(annotations, "readOnlyHint", False))

            async def handler(
                payload: dict[str, Any],
                context: ToolContext,
                *,
                server_spec: McpServerSpec = spec,
                remote_name: str = tool_name,
            ) -> ToolResult:
                _ = context
                return await self.call_server_tool(server_spec, remote_name, payload)

            wrapped.append(
                Tool(
                    name=local_name,
                    description=remote_tool.description or f"MCP tool {tool_name}",
                    parameters=schema,
                    handler=handler,
                    is_read_only=read_only,
                    is_concurrency_safe=False,
                    danger_level="safe" if read_only else "medium",
                    requires_approval=not read_only,
                )
            )
        return wrapped

    def _virtual_resource_tools(self, spec: McpServerSpec) -> list[Tool]:
        async def list_handler(payload: dict[str, Any], context: ToolContext) -> ToolResult:
            _ = payload, context
            return await self.list_resources(spec)

        async def read_handler(payload: dict[str, Any], context: ToolContext) -> ToolResult:
            _ = context
            return await self.read_resource(spec, str(payload["uri"]))

        return [
            Tool(
                name=f"mcp__{spec.name}__list_resources",
                description=f"List MCP resources from {spec.name}.",
                parameters=object_schema({}),
                handler=list_handler,
                is_read_only=True,
            ),
            Tool(
                name=f"mcp__{spec.name}__read_resource",
                description=f"Read an MCP resource from {spec.name}.",
                parameters=object_schema(
                    {"uri": {"type": "string", "description": "Resource URI"}},
                    ["uri"],
                ),
                required_keys=["uri"],
                handler=read_handler,
                is_read_only=True,
            ),
        ]

    def _virtual_prompt_tools(self, spec: McpServerSpec) -> list[Tool]:
        async def list_handler(payload: dict[str, Any], context: ToolContext) -> ToolResult:
            _ = payload, context
            return await self.list_prompts(spec)

        async def get_handler(payload: dict[str, Any], context: ToolContext) -> ToolResult:
            _ = context
            arguments = payload.get("arguments")
            if arguments is not None and not isinstance(arguments, dict):
                return ToolResult("arguments must be an object", is_error=True)
            return await self.get_prompt(
                spec,
                str(payload["name"]),
                {str(k): str(v) for k, v in (arguments or {}).items()},
            )

        return [
            Tool(
                name=f"mcp__{spec.name}__list_prompts",
                description=f"List MCP prompts from {spec.name}.",
                parameters=object_schema({}),
                handler=list_handler,
                is_read_only=True,
            ),
            Tool(
                name=f"mcp__{spec.name}__get_prompt",
                description=f"Get an MCP prompt from {spec.name}.",
                parameters=object_schema(
                    {
                        "name": {"type": "string", "description": "Prompt name"},
                        "arguments": {"type": "object", "description": "Prompt arguments"},
                    },
                    ["name"],
                ),
                required_keys=["name"],
                handler=get_handler,
                is_read_only=True,
            ),
        ]

    @asynccontextmanager
    async def _session(self, spec: McpServerSpec):
        if spec.type in {"stdio", "local"}:
            if not spec.command:
                raise ValueError(f"MCP server {spec.name} is missing command")
            params = StdioServerParameters(
                command=spec.command,
                args=spec.args,
                env={**os.environ, **spec.env},
                cwd=spec.cwd or self.project_root,
            )
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with self._log_path(spec.name).open("a", encoding="utf-8") as errlog:
                async with (
                    stdio_client(params, errlog=errlog) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    yield session
            return
        if spec.type in {"http", "streamable_http", "streamable-http"}:
            if not spec.url:
                raise ValueError(f"MCP server {spec.name} is missing url")
            async with (
                streamablehttp_client(
                    spec.url,
                    headers=spec.headers or None,
                    timeout=spec.timeout,
                ) as (read, write, _session_id),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                yield session
            return
        raise ValueError(f"Unsupported MCP transport: {spec.type}")

    def _set_enabled(self, name: str, enabled: bool) -> bool:
        spec = self.specs.get(name)
        if not spec:
            return False
        data = _read_project_config(self.project_config_path)
        servers = data.setdefault("mcpServers", {})
        raw = servers.setdefault(name, _spec_to_raw(spec))
        if not isinstance(raw, dict):
            raw = _spec_to_raw(spec)
            servers[name] = raw
        raw["enabled"] = enabled
        self.project_config_path.parent.mkdir(parents=True, exist_ok=True)
        self.project_config_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.specs = load_mcp_server_specs(self.project_root)
        return True

    def _log_path(self, name: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in name)
        return self.log_dir / f"{safe}.log"


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(filter(None, (_content_to_text(item) for item in content)))
    if hasattr(content, "text"):
        return str(content.text)
    if hasattr(content, "data") and hasattr(content, "mimeType"):
        data = str(content.data)
        return f"[image {content.mimeType} base64 chars={len(data)}]"
    if hasattr(content, "resource"):
        return _content_to_text(content.resource)
    if hasattr(content, "model_dump"):
        return json.dumps(content.model_dump(mode="json"), ensure_ascii=False)
    return str(content)


def _read_project_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"mcpServers": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"mcpServers": {}}
    if not isinstance(data, dict):
        return {"mcpServers": {}}
    data.setdefault("mcpServers", {})
    return data


def _spec_to_raw(spec: McpServerSpec) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "type": spec.type,
        "enabled": spec.enabled,
        "timeout": spec.timeout,
    }
    if spec.command:
        raw["command"] = spec.command
    if spec.args:
        raw["args"] = spec.args
    if spec.env:
        raw["env"] = spec.env
    if spec.cwd:
        raw["cwd"] = spec.cwd
    if spec.url:
        raw["url"] = spec.url
    if spec.headers:
        raw["headers"] = spec.headers
    return raw
