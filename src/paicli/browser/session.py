from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen


@dataclass(slots=True)
class BrowserState:
    mode: str
    browser_url: str | None = None
    config_path: Path | None = None


@dataclass(slots=True)
class BrowserTab:
    id: str
    title: str
    url: str


class BrowserSession:
    def __init__(self, project_root: str | Path):
        self.project_root = Path(project_root).resolve()
        self.config_path = self.project_root / ".paicli" / "mcp.json"

    def status(self) -> BrowserState:
        args = self._chrome_args()
        browser_url = _browser_url(args)
        if "--autoConnect" in args or browser_url:
            return BrowserState("shared", browser_url=browser_url, config_path=self.config_path)
        return BrowserState("isolated", config_path=self.config_path)

    def connect(self, port: int | None = None) -> BrowserState:
        if port is not None and not 1024 <= port <= 65535:
            raise ValueError("CDP port must be in 1024-65535")
        browser_url = f"http://127.0.0.1:{port}" if port is not None else None
        args = _base_chrome_args()
        if browser_url:
            args.append(f"--browser-url={browser_url}")
        else:
            args.append("--autoConnect")
        self._write_chrome_args(args)
        return BrowserState("shared", browser_url=browser_url, config_path=self.config_path)

    def disconnect(self) -> BrowserState:
        args = _base_chrome_args()
        args.append("--isolated=true")
        self._write_chrome_args(args)
        return BrowserState("isolated", config_path=self.config_path)

    def tabs(
        self,
        *,
        fetch_json: Callable[[str], list[dict]] | None = None,
    ) -> list[BrowserTab]:
        browser_url = self.status().browser_url
        if not browser_url:
            return []
        fetch = fetch_json or _fetch_json
        raw_tabs = fetch(browser_url.rstrip("/") + "/json/list")
        tabs = []
        for item in raw_tabs:
            if not isinstance(item, dict):
                continue
            tabs.append(
                BrowserTab(
                    id=str(item.get("id") or ""),
                    title=str(item.get("title") or ""),
                    url=str(item.get("url") or ""),
                )
            )
        return tabs

    def _chrome_args(self) -> list[str]:
        data = self._read()
        raw = data.get("mcpServers", {}).get("chrome-devtools", {}).get("args") or []
        return [str(item) for item in raw]

    def _write_chrome_args(self, args: list[str]) -> None:
        data = self._read()
        servers = data.setdefault("mcpServers", {})
        servers["chrome-devtools"] = {
            "type": "stdio",
            "command": "npx",
            "args": args,
        }
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _read(self) -> dict:
        if not self.config_path.exists():
            return {"mcpServers": {}}
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"mcpServers": {}}
        if not isinstance(data, dict):
            return {"mcpServers": {}}
        data.setdefault("mcpServers", {})
        return data


def _base_chrome_args() -> list[str]:
    return ["-y", "chrome-devtools-mcp@latest", "--no-usage-statistics"]


def _browser_url(args: list[str]) -> str | None:
    for arg in args:
        if arg.startswith("--browser-url="):
            return arg.split("=", 1)[1]
    return None


def _fetch_json(url: str) -> list[dict]:
    with urlopen(url, timeout=5) as response:  # noqa: S310 - URL is local CDP config
        data = json.loads(response.read().decode("utf-8"))
    return data if isinstance(data, list) else []
