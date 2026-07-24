from __future__ import annotations

from pathlib import Path

from paicli.config import PaiCliConfig
from paicli.mcp import McpClientManager
from paicli.tools import ToolRegistry, get_builtin_tools


async def build_tool_registry(
    *,
    config: PaiCliConfig,
    cwd: str,
    mcp_config_paths: list[str | Path] | None = None,
) -> tuple[ToolRegistry, McpClientManager | None]:
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    manager: McpClientManager | None = None
    if config.features.mcp:
        manager = McpClientManager(cwd, config_paths=mcp_config_paths)
        registry.register_all(await manager.load_tools())
    return registry, manager
