"""Query runtime CLI commands."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import typer

from mimir.adapters.cli_support.common import (
    DEFAULT_CONFIG,
    console,
    load_config,
    setup_logging,
    setup_stdio_logging,
    stderr_console,
)
from mimir.domain.errors import NoActiveIndexError
from mimir.runtime import RuntimeFactory

query_app = typer.Typer(
    name="query",
    help="Serve and query published Mimir indexes.",
    no_args_is_help=True,
)


@query_app.command()
def search(
    query: str = typer.Argument(..., help="Natural language query"),
    budget: int = typer.Option(8000, "--budget", "-b", help="Token budget"),
    repos: Optional[str] = typer.Option(None, "--repos", "-r", help="Comma-separated repo filter"),
    flat: bool = typer.Option(False, "--flat", help="Force flat search"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Search the active published index."""
    setup_logging(verbose)
    cfg, _ = load_config(config, workspace)
    runtime = RuntimeFactory(cfg).query()
    try:
        graph = runtime.require_graph()
        bundle = asyncio.run(runtime.retrieval.search(
            query=query,
            graph=graph,
            token_budget=budget,
            repos=repos.split(",") if repos else None,
            flat=flat,
        ))
        console.print(f"\n[bold]{bundle.summary}[/]")
        if bundle.session_note:
            console.print(f"[dim]{bundle.session_note}[/]")
        console.print(f"[dim]Tokens: {bundle.token_count}[/]\n")
        console.print(bundle.format_for_llm())
    except NoActiveIndexError as exc:
        console.print(f"[red bold]{exc}.[/] Run: mimir indexer run")
        raise typer.Exit(1) from exc
    except Exception as exc:
        console.print(f"[red bold]Search failed:[/] {exc}")
        raise typer.Exit(1) from exc
    finally:
        runtime.close()


@query_app.command()
def status(
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Show the active published index."""
    setup_logging(verbose)
    cfg, _ = load_config(config, workspace)
    runtime = RuntimeFactory(cfg).query()
    try:
        status_data = runtime.status()
        console.print(f"Active version: {status_data['active_version'] or '-'}")
        console.print(f"Nodes: {status_data['graph_nodes']}")
        console.print(f"Edges: {status_data['graph_edges']}")
        if status_data["activated_at"]:
            console.print(f"Activated: {status_data['activated_at']}")
    finally:
        runtime.close()


@query_app.command()
def serve(
    http: bool = typer.Option(False, "--http", help="Start query HTTP server"),
    http_port: int = typer.Option(8421, "--http-port", help="Port for the HTTP server"),
    http_host: str = typer.Option("0.0.0.0", "--http-host", help="Host to bind"),
    remote: Optional[str] = typer.Option(None, "--remote", "-r", help="URL of a remote Mimir HTTP server to proxy"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Start query serving locally or proxy to a remote query server."""
    if http and remote:
        console.print("[red bold]Error:[/] --http and --remote are mutually exclusive.")
        raise typer.Exit(1)

    setup_stdio_logging(verbose)
    if remote:
        stderr_console.print(f"[green]Connecting to remote Mimir server at {remote}[/]")
        from mimir.adapters.mcp.remote import run_remote_mcp

        run_remote_mcp(remote)
        return

    cfg, workspace_name = load_config(config, workspace)
    if http:
        stderr_console.print(f"[green]Starting Mimir query HTTP server on http://{http_host}:{http_port}[/]")
        from mimir.adapters.http import run_http_server

        run_http_server(cfg, host=http_host, port=http_port, workspace_name=workspace_name)
        return

    from mimir.adapters.mcp.server import run_mcp_server

    run_mcp_server(cfg, workspace_name=workspace_name)


@query_app.command()
def ui(
    port: int = typer.Option(8420, "--port", "-p", help="HTTP port"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Launch the read-only web inspector over the active index."""
    setup_logging(verbose)
    cfg, _ = load_config(config, workspace)
    from mimir.adapters.web.server import run_web_server

    console.print(f"[green]Starting Mimir Inspector at http://localhost:{port}[/]")
    run_web_server(cfg, port=port)
