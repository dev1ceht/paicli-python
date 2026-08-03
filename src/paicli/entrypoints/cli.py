from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from paicli import __version__
from paicli.agent import QueryEngine
from paicli.benchmark import (
    DEFAULT_MAX_ELAPSED_SECONDS,
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_MAX_TOTAL_TOKENS,
    DEFAULT_MAX_TURNS,
    BenchmarkOptions,
    run_benchmark_prompt,
)
from paicli.bootstrap import build_tool_registry
from paicli.config import get_config_paths, load_benchmark_config, load_config
from paicli.entrypoints.repl import start_repl
from paicli.llm import create_llm_client
from paicli.mcp import load_mcp_server_specs, serve_http, serve_stdio, write_chrome_devtools_config
from paicli.runtime import RuntimeApiServer
from paicli.runtime.api import runtime_api_key
from paicli.thinking import THINKING_LEVEL_SET

app = typer.Typer(
    name="paicli",
    help="PaiCLI — Terminal AI Agent in Python",
    invoke_without_command=True,
    no_args_is_help=False,
)
mcp_app = typer.Typer(help="MCP server management")
app.add_typer(mcp_app, name="mcp")
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"paicli {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    prompt: Annotated[
        str | None,
        typer.Option("-p", "--prompt", help="Print mode: single prompt, non-interactive"),
    ] = None,
    model: Annotated[str | None, typer.Option("-m", "--model", help="Override model name")] = None,
    provider: Annotated[
        str | None,
        typer.Option("--provider", help="Override LLM provider"),
    ] = None,
    thinking: Annotated[
        str | None,
        typer.Option(
            "--thinking",
            help="Set thinking level: off, on, minimal, low, medium, high, xhigh, max",
        ),
    ] = None,
    plain: Annotated[bool, typer.Option("--plain", help="Use plain text rendering")] = False,
    cwd: Annotated[Path | None, typer.Option("--cwd", help="Working directory")] = None,
    benchmark: Annotated[
        bool,
        typer.Option("--benchmark", help="Run one unattended Harbor benchmark turn"),
    ] = False,
    benchmark_log_dir: Annotated[
        Path | None,
        typer.Option("--benchmark-log-dir", help="Directory for benchmark diagnostics"),
    ] = None,
    max_turns: Annotated[int | None, typer.Option("--max-turns")] = None,
    max_tool_calls: Annotated[int | None, typer.Option("--max-tool-calls")] = None,
    max_elapsed_seconds: Annotated[
        float | None,
        typer.Option("--max-elapsed-seconds"),
    ] = None,
    max_total_tokens: Annotated[int | None, typer.Option("--max-total-tokens")] = None,
    benchmark_source_identity: Annotated[
        Path | None,
        typer.Option("--benchmark-source-identity", hidden=True),
    ] = None,
    benchmark_mcp_config: Annotated[
        Path | None,
        typer.Option("--benchmark-mcp-config", hidden=True),
    ] = None,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version"),
    ] = False,
) -> None:
    _ = version
    if ctx.invoked_subcommand is not None:
        return
    root = (cwd or Path.cwd()).resolve()
    if thinking is not None:
        thinking = thinking.strip().lower()
        if thinking not in THINKING_LEVEL_SET:
            values = ", ".join(sorted(THINKING_LEVEL_SET))
            raise typer.BadParameter(f"thinking must be one of: {values}")
    benchmark_only_values = (
        benchmark_log_dir,
        max_turns,
        max_tool_calls,
        max_elapsed_seconds,
        max_total_tokens,
        benchmark_source_identity,
        benchmark_mcp_config,
    )
    if not benchmark and any(value is not None for value in benchmark_only_values):
        raise typer.BadParameter("benchmark resource options require --benchmark")
    if benchmark:
        if prompt is None:
            raise typer.BadParameter("--benchmark requires --prompt")
        if provider is not None or model is not None:
            raise typer.BadParameter(
                "benchmark provider and model must be supplied through "
                "PAICLI_* environment variables"
            )
        limits = {
            "max_turns": DEFAULT_MAX_TURNS if max_turns is None else max_turns,
            "max_tool_calls": (
                DEFAULT_MAX_TOOL_CALLS if max_tool_calls is None else max_tool_calls
            ),
            "max_elapsed_seconds": (
                DEFAULT_MAX_ELAPSED_SECONDS if max_elapsed_seconds is None else max_elapsed_seconds
            ),
            "max_total_tokens": (
                DEFAULT_MAX_TOTAL_TOKENS if max_total_tokens is None else max_total_tokens
            ),
        }
        if any(value <= 0 for value in limits.values()):
            raise typer.BadParameter("benchmark resource limits must be positive")
        log_dir = benchmark_log_dir or Path(
            os.getenv("PAICLI_BENCHMARK_LOG_DIR", ".paicli/benchmark")
        )
        benchmark_overrides: dict[str, object] = {"render_mode": "plain"}
        if thinking is not None:
            benchmark_overrides["llm"] = {"thinking_level": thinking}
        config = load_benchmark_config(overrides=benchmark_overrides)
        options = BenchmarkOptions(
            log_dir=log_dir,
            source_identity_file=benchmark_source_identity,
            mcp_config_file=benchmark_mcp_config,
            **limits,
        )
        try:
            result = asyncio.run(
                run_benchmark_prompt(prompt, cwd=root, config=config, options=options)
            )
        except Exception as exc:  # noqa: BLE001 - CLI boundary reports benchmark failures
            typer.echo(f"Fatal error: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(result)
        return
    overrides: dict = {}
    if provider or model or plain or thinking is not None:
        overrides = {
            "llm": {"provider": provider, "model": model},
            "render_mode": "plain" if plain else None,
        }
        if thinking is not None:
            overrides["llm"]["thinking_level"] = thinking
    config = load_config(project_root=root, overrides=overrides)
    if plain:
        config.render_mode = "plain"
    if prompt is not None:
        asyncio.run(_run_prompt(prompt, str(root), config))
    else:
        asyncio.run(start_repl(str(root), config, cli_thinking_level=thinking))


@app.command("doctor")
def doctor(
    cwd: Annotated[Path | None, typer.Option("--cwd", help="Working directory")] = None,
) -> None:
    root = (cwd or Path.cwd()).resolve()
    config = load_config(project_root=root)
    checks = {
        "python": sys.version.split()[0],
        "uv": shutil.which("uv") or "missing",
        "node": _version_of("node"),
        "npx": shutil.which("npx") or "missing",
        "rg": shutil.which("rg") or "missing",
        "api_key": "configured" if config.llm.api_key else "missing",
        "provider": config.llm.provider,
        "model": config.llm.model,
        "cwd": str(root),
        "config_paths": [str(path) for path in get_config_paths(root)],
    }
    console.print_json(json.dumps(checks, ensure_ascii=False))


@app.command("serve")
def runtime_serve(
    http: Annotated[bool, typer.Option("--http", help="Serve Runtime API over HTTP")] = True,
    port: Annotated[int, typer.Option("--port", help="HTTP port")] = 8080,
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", help="Runtime API key. Defaults to PAICLI_RUNTIME_API_KEY."),
    ] = None,
    cwd: Annotated[Path | None, typer.Option("--cwd", help="Working directory")] = None,
) -> None:
    _ = http
    root = (cwd or Path.cwd()).resolve()
    config = load_config(project_root=root)
    try:
        key = runtime_api_key(api_key)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    RuntimeApiServer(cwd=str(root), config=config, api_key=key, port=port).serve_forever()


@mcp_app.command("serve")
def mcp_serve(
    transport: Annotated[
        str,
        typer.Option("--transport", help="Transport type: stdio or http"),
    ] = "stdio",
    port: Annotated[int, typer.Option("--port", help="HTTP port")] = 3000,
    cwd: Annotated[Path | None, typer.Option("--cwd", help="Working directory")] = None,
) -> None:
    root = str((cwd or Path.cwd()).resolve())
    if transport == "http":
        serve_http(port=port, cwd=root)
    elif transport == "stdio":
        asyncio.run(serve_stdio(cwd=root))
    else:
        raise typer.BadParameter("transport must be stdio or http")


@mcp_app.command("init-chrome")
def mcp_init_chrome(
    scope: Annotated[
        str,
        typer.Option("--scope", help="Config scope: user or project"),
    ] = "project",
    cwd: Annotated[Path | None, typer.Option("--cwd", help="Working directory")] = None,
    browser_url: Annotated[
        str | None,
        typer.Option("--browser-url", help="Connect to an existing Chrome remote debugging URL"),
    ] = None,
    headless: Annotated[bool, typer.Option("--headless", help="Start Chrome headless")] = False,
    slim: Annotated[bool, typer.Option("--slim", help="Use Chrome DevTools slim mode")] = False,
) -> None:
    if scope not in {"user", "project"}:
        raise typer.BadParameter("scope must be user or project")
    root = None if scope == "user" else (cwd or Path.cwd()).resolve()
    path = write_chrome_devtools_config(
        scope_root=root,
        browser_url=browser_url,
        headless=headless,
        slim=slim,
    )
    typer.echo(f"Wrote Chrome DevTools MCP config to {path}")


@mcp_app.command("list")
def mcp_list(
    cwd: Annotated[Path | None, typer.Option("--cwd", help="Working directory")] = None,
) -> None:
    root = (cwd or Path.cwd()).resolve()
    specs = load_mcp_server_specs(root)
    if not specs:
        typer.echo("No MCP servers configured.")
        return
    for spec in specs.values():
        target = spec.url or f"{spec.command} {' '.join(spec.args)}".strip()
        typer.echo(f"{spec.name}\t{spec.type}\t{target}")


async def _run_prompt(prompt: str, cwd: str, config) -> None:
    config.render_mode = "plain"
    if not config.llm.api_key:
        typer.echo(
            "Fatal error: PAICLI_API_KEY is not configured. Set it in env, "
            "~/.paicli/config.json, or project .paicli/config.json.",
            err=True,
        )
        raise typer.Exit(1)
    registry, manager = await build_tool_registry(config=config, cwd=cwd)
    if manager and manager.last_errors:
        for name, error in manager.last_errors.items():
            typer.echo(f"MCP server {name} failed to load: {error}", err=True)
    engine = QueryEngine(
        llm_client=create_llm_client(
            config.llm,
            retry_policy=config.retry.resolve("llm"),
            retry_audit_path=config.policy.audit_log_path,
            retry_cwd=cwd,
        ),
        tool_registry=registry,
        config=config,
        cwd=cwd,
    )
    try:
        result = await engine.ask_complete_async(prompt)
    except Exception as exc:  # noqa: BLE001 - CLI should report model/config errors cleanly
        typer.echo(f"Fatal error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(result.text)


def _version_of(command: str) -> str:
    if not shutil.which(command):
        return "missing"
    try:
        result = subprocess.run(
            [command, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:  # noqa: BLE001
        return "unknown"
    return (result.stdout or result.stderr).strip() or "unknown"
