"""Runtime and maintenance CLI commands."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from mimir.adapters.cli_support.common import (
    DEFAULT_CONFIG,
    console,
    enable_watcher,
    load_config,
    setup_logging,
    setup_stdio_logging,
    stderr_console,
)


def register(app: typer.Typer) -> None:
    @app.command()
    def serve(
        http: bool = typer.Option(False, "--http", help="Start as shared HTTP server (for team access)"),
        http_port: int = typer.Option(8421, "--http-port", help="Port for the HTTP server (only with --http)"),
        http_host: str = typer.Option("0.0.0.0", "--http-host", help="Host to bind the HTTP server to"),
        remote: Optional[str] = typer.Option(None, "--remote", "-r", help="URL of a remote Mimir HTTP server to proxy"),
        watch: bool = typer.Option(False, "--watch", help="Enable file watcher for live re-indexing"),
        workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
        config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
        verbose: bool = typer.Option(False, "--verbose", "-v"),
    ) -> None:
        """Start the MCP server."""
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
        if watch:
            cfg.watcher = enable_watcher(cfg.watcher)

        if http:
            stderr_console.print(f"[green]Starting shared Mimir HTTP server on http://{http_host}:{http_port}[/]")
            stderr_console.print(f"[dim]Mobile/frontend devs connect with: mimir serve --remote http://<this-host>:{http_port}[/]")
            from mimir.adapters.http import run_http_server

            run_http_server(cfg, host=http_host, port=http_port, workspace_name=workspace_name)
            return

        from mimir.adapters.mcp.server import run_mcp_server

        run_mcp_server(cfg, workspace_name=workspace_name)

    @app.command()
    def ui(
        port: int = typer.Option(8420, "--port", "-p", help="HTTP port"),
        workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
        config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
        verbose: bool = typer.Option(False, "--verbose", "-v"),
    ) -> None:
        """Launch the web inspector UI."""
        setup_logging(verbose)
        cfg, _ = load_config(config, workspace)
        from mimir.adapters.web.server import run_web_server

        console.print(f"[green]Starting Mimir Inspector at http://localhost:{port}[/]")
        run_web_server(cfg, port=port)

    @app.command()
    def clear(
        graph: bool = typer.Option(True, help="Clear the code graph and embeddings"),
        sessions: bool = typer.Option(True, help="Clear all conversation sessions"),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
        workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
        config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
        verbose: bool = typer.Option(False, "--verbose", "-v"),
    ) -> None:
        """Delete locally stored index data."""
        from mimir.container import Container

        setup_logging(verbose)
        targets = []
        if graph:
            targets.append("code graph + embeddings")
        if sessions:
            targets.append("sessions")
        if not targets:
            console.print("[yellow]Nothing to clear — use --graph and/or --sessions[/]")
            raise typer.Exit(0)
        if not yes:
            console.print(f"[red bold]This will permanently delete:[/] {', '.join(targets)}")
            if not typer.confirm("Continue?"):
                console.print("[yellow]Aborted[/]")
                raise typer.Exit(0)

        cfg, _ = load_config(config, workspace)
        container = Container(cfg)
        try:
            result = container.clear_data(graph=graph, sessions=sessions)
            console.print(f"[green]✓ Cleared:[/] {', '.join(result['cleared'])}")
        except Exception as exc:
            console.print(f"[red bold]Clear failed:[/] {exc}")
            raise typer.Exit(1) from exc
        finally:
            container.close()

    @app.command()
    def vacuum(
        workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
        config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
        verbose: bool = typer.Option(False, "--verbose", "-v"),
    ) -> None:
        """Compact the SQLite graph database to reclaim unused file space."""
        from mimir.container import Container

        setup_logging(verbose)
        cfg, _ = load_config(config, workspace)
        container = Container(cfg)
        try:
            container.graph_store.vacuum()
            console.print("[green]✓ Database vacuumed successfully[/]")
        except Exception as exc:
            console.print(f"[red bold]Vacuum failed:[/] {exc}")
            raise typer.Exit(1) from exc
        finally:
            container.close()

    @app.command()
    def init(
        path: Path = typer.Option(Path("."), "--path", help="Directory to initialise"),
    ) -> None:
        """Create a mimir.toml configuration file."""
        config_path = path / "mimir.toml"
        if config_path.exists():
            console.print(f"[yellow]Config already exists: {config_path}[/]")
            raise typer.Exit(0)

        template = '''# Mimir v1 Configuration

[[repos]]
name = "my-project"
path = "."
language_hint = "python"

[indexing]
summary_mode = "heuristic"
excluded_patterns = ["__pycache__", "node_modules", ".git", "venv", ".venv"]
max_file_size_kb = 500

[embeddings]
model = "local:all-MiniLM-L6-v2"

[vector_db]
backend = "chroma"

[retrieval]
default_beam_width = 3
default_token_budget = 8000
expansion_hops = 2

[temporal]
recency_lambda = 0.02
co_retrieval_enabled = true
'''
        config_path.write_text(template)
        console.print(f"[green]✓ Created {config_path}[/]")
        console.print("  Edit the [[repos]] section to point to your code repositories.")
