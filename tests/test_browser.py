from __future__ import annotations

import json

from paicli.browser import BrowserSession
from paicli.config import load_config
from paicli.tools import get_builtin_tools
from paicli.tools.base import ToolContext


def test_browser_connect_shared_writes_auto_connect_mcp_config(tmp_path):
    session = BrowserSession(tmp_path)

    state = session.connect()

    assert state.mode == "shared"
    data = json.loads((tmp_path / ".paicli" / "mcp.json").read_text(encoding="utf-8"))
    args = data["mcpServers"]["chrome-devtools"]["args"]
    assert "--autoConnect" in args
    assert not any(arg.startswith("--browser-url=") for arg in args)
    assert "--isolated=true" not in args


def test_browser_connect_port_writes_browser_url_mcp_config(tmp_path):
    session = BrowserSession(tmp_path)

    state = session.connect(port=9222)

    assert state.mode == "shared"
    assert state.browser_url == "http://127.0.0.1:9222"
    data = json.loads((tmp_path / ".paicli" / "mcp.json").read_text(encoding="utf-8"))
    args = data["mcpServers"]["chrome-devtools"]["args"]
    assert "--browser-url=http://127.0.0.1:9222" in args
    assert "--autoConnect" not in args


def test_browser_disconnect_restores_isolated_mcp_config(tmp_path):
    session = BrowserSession(tmp_path)
    session.connect()

    state = session.disconnect()

    assert state.mode == "isolated"
    data = json.loads((tmp_path / ".paicli" / "mcp.json").read_text(encoding="utf-8"))
    args = data["mcpServers"]["chrome-devtools"]["args"]
    assert "--isolated=true" in args
    assert "--autoConnect" not in args
    assert not any(arg.startswith("--browser-url=") for arg in args)


def test_browser_tabs_reads_cdp_json_list(tmp_path):
    session = BrowserSession(tmp_path)
    session.connect(port=9222)

    tabs = session.tabs(
        fetch_json=lambda url: (
            [
                {"id": "page-1", "title": "Home", "url": "https://example.com"},
            ]
            if url == "http://127.0.0.1:9222/json/list"
            else []
        )
    )

    assert [(tab.id, tab.title, tab.url) for tab in tabs] == [
        ("page-1", "Home", "https://example.com")
    ]


def test_browser_tools_update_session_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    tools = {tool.name: tool for tool in get_builtin_tools()}
    config = load_config(project_root=tmp_path)
    context = ToolContext(cwd=str(tmp_path), config=config)

    import asyncio

    result = asyncio.run(tools["browser_connect"].execute({}, context))
    status = asyncio.run(tools["browser_status"].execute({}, context))
    asyncio.run(tools["browser_disconnect"].execute({}, context))

    assert "shared" in result.content
    assert "shared" in status.content
    args = json.loads((tmp_path / ".paicli" / "mcp.json").read_text(encoding="utf-8"))[
        "mcpServers"
    ]["chrome-devtools"]["args"]
    assert "--isolated=true" in args
