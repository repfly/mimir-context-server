"""Typer CLI — primary driving adapter for Mimir.

All commands are thin wrappers that initialise the ``Container``
from ``mimir.toml`` and delegate to the appropriate service.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.tree import Tree

from mimir.domain.config import ConfigError, MimirConfig

app = typer.Typer(
    name="mimir",
    help="Mimir — Context Server v1 — Context Engine for Large-Scale Codebases",
    no_args_is_help=True,
)
console = Console()

# Default config path
_DEFAULT_CONFIG = Path("mimir.toml")
_NO_WORKSPACE = ""  # sentinel for Optional workspace flag


def _resolve_config_path(
    config: Path,
    workspace: Optional[str] = None,
) -> tuple[Path, Optional[str]]:
    """Resolve the config path and workspace name, enforcing mutual exclusivity.

    Returns (config_path, workspace_name_or_None).
    """
    err_console = Console(stderr=True)

    # Check for --workspace via env var fallback
    resolved_workspace = workspace or os.environ.get("MIMIR_WORKSPACE") or None

    explicit_config = config != _DEFAULT_CONFIG  # user passed --config explicitly

    if resolved_workspace and explicit_config:
        err_console.print(
            "[red bold]Error:[/] --workspace and --config are mutually exclusive. "
            "Use one or the other."
        )
        raise typer.Exit(1)

    if resolved_workspace:
        from mimir.domain.workspace import WorkspaceRegistry
        try:
            registry = WorkspaceRegistry()
            config_path = registry.resolve(resolved_workspace)
        except ConfigError as exc:
            err_console.print(f"[red bold]Workspace error:[/] {exc}")
            raise typer.Exit(1) from exc
        return config_path, resolved_workspace

    return config, None


def _load_config(
    config: Path,
    workspace: Optional[str] = None,
) -> tuple[MimirConfig, Optional[str]]:
    """Load and validate config with user-friendly error messages.

    Returns (config, workspace_name_or_None).
    """
    config_path, ws_name = _resolve_config_path(config, workspace)
    try:
        return MimirConfig.load(config_path), ws_name
    except ConfigError as exc:
        err_console = Console(stderr=True)
        err_console.print(f"[red bold]Config error:[/] {exc}")
        raise typer.Exit(1) from exc


def _setup_logging(verbose: bool) -> None:
    from rich.logging import RichHandler
    level = logging.DEBUG if verbose else logging.INFO
    handler = RichHandler(rich_tracebacks=True, console=console, markup=False, show_path=False)
    handler.setFormatter(logging.Formatter("%(name)s — %(message)s", datefmt="[%X]"))
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)
    logging.getLogger("aiohttp.access").setLevel(logging.ERROR)


# ------------------------------------------------------------------
# workspace sub-app
# ------------------------------------------------------------------

workspace_app = typer.Typer(
    name="workspace",
    help="Manage named Mimir workspaces (~/.mimir/workspaces.toml).",
    no_args_is_help=True,
)
app.add_typer(workspace_app, name="workspace")


@workspace_app.command("add")
def workspace_add(
    name: str = typer.Argument(..., help="Workspace name (letters, digits, hyphens, underscores)"),
    config: Path = typer.Option(
        _DEFAULT_CONFIG, "--config", "-c",
        help="Path to mimir.toml (default: ./mimir.toml)",
    ),
) -> None:
    """Register a workspace in the global registry."""
    from mimir.domain.workspace import WorkspaceRegistry
    registry = WorkspaceRegistry()
    try:
        registry.add(name, config)
        console.print(f"[green]✓ Registered workspace:[/] {name} → {config.resolve()}")
        console.print(f"  Registry: {registry._path}")
    except ConfigError as exc:
        console.print(f"[red bold]Error:[/] {exc}")
        raise typer.Exit(1) from exc


@workspace_app.command("list")
def workspace_list() -> None:
    """List all registered workspaces."""
    from mimir.domain.workspace import WorkspaceRegistry
    registry = WorkspaceRegistry()
    workspaces = registry.list()

    if not workspaces:
        console.print("[yellow]No workspaces registered.[/] Use [bold]mimir workspace add[/] to register one.")
        return

    table = Table(title=f"Registered Workspaces ({registry._path})")
    table.add_column("Name", style="cyan bold")
    table.add_column("Config Path", style="green")
    table.add_column("Exists?", style="yellow")
    for name, path in sorted(workspaces.items()):
        exists = "✓" if path.is_file() else "✗ missing"
        table.add_row(name, str(path), exists)
    console.print(table)


@workspace_app.command("remove")
def workspace_remove(
    name: str = typer.Argument(..., help="Workspace name to remove"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Unregister a workspace from the global registry."""
    from mimir.domain.workspace import WorkspaceRegistry
    registry = WorkspaceRegistry()
    if not yes:
        confirmed = typer.confirm(f"Remove workspace '{name}' from registry?")
        if not confirmed:
            console.print("[yellow]Aborted[/]")
            raise typer.Exit(0)
    try:
        registry.remove(name)
        console.print(f"[green]✓ Removed workspace:[/] {name}")
    except ConfigError as exc:
        console.print(f"[red bold]Error:[/] {exc}")
        raise typer.Exit(1) from exc



# ------------------------------------------------------------------
# index
# ------------------------------------------------------------------

def _kill_serve_processes() -> int:
    """Find and SIGTERM any running ``mimir serve`` processes, including
    Docker containers that bind-mount the current project directory.

    Returns the number of processes/containers stopped.
    """
    import os
    import signal
    import subprocess

    current_pid = os.getpid()
    killed = 0

    # 1. Kill local (non-Docker) mimir serve processes
    try:
        result = subprocess.run(
            ["pgrep", "-f", "mimir.*serve"],
            capture_output=True,
            text=True,
        )
        pids = [int(p) for p in result.stdout.strip().splitlines() if p.strip()]

        for pid in pids:
            if pid == current_pid:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                killed += 1
            except ProcessLookupError:
                pass  # already gone
            except PermissionError:
                pass  # not ours to kill
    except FileNotFoundError:
        pass  # pgrep not available (Windows?)

    # 2. Stop Docker containers running mimir that mount this project directory.
    #    A running container with a bind-mount holds open SQLite file handles;
    #    if we delete chroma files while those handles are live, SQLite shifts
    #    into readonly mode (error code 1032).
    try:
        cwd = os.getcwd()
        # List running containers whose command contains "mimir"
        result = subprocess.run(
            ["docker", "ps", "--filter", "status=running",
             "--format", "{{.ID}}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for container_id in result.stdout.strip().splitlines():
                if not container_id.strip():
                    continue
                # Inspect the container's bind mounts
                inspect = subprocess.run(
                    ["docker", "inspect", "--format",
                     '{{range .Mounts}}{{.Source}}::{{.Destination}} {{end}}',
                     container_id.strip()],
                    capture_output=True, text=True, timeout=5,
                )
                if inspect.returncode == 0 and cwd in inspect.stdout:
                    subprocess.run(
                        ["docker", "stop", container_id.strip()],
                        capture_output=True, text=True, timeout=15,
                    )
                    killed += 1
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # docker not installed or timed out

    return killed

@app.command()
def index(
    mode: Optional[str] = typer.Option(None, help="Summary mode: none, heuristic, llm"),
    clean: bool = typer.Option(False, "--clean", help="Force a full re-index (wipes existing data)"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
    config: Path = typer.Option(_DEFAULT_CONFIG, "--config", "-c", help="Config file path"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Index all configured repositories. Uses incremental indexing by default."""
    _setup_logging(verbose)

    # Stop any running `mimir serve` instances to release database locks.
    killed = _kill_serve_processes()
    if killed:
        import time
        console.print(
            f"[yellow]⚠ Stopped {killed} running 'mimir serve' process(es) to release "
            f"database locks. Re-run [bold]mimir serve[/] after indexing completes.[/]"
        )
        time.sleep(0.5)  # brief pause so processes can flush & release file handles

    cfg, _ = _load_config(config, workspace)

    # For --clean, we must wipe the ChromaDB directory BEFORE Container() is created.
    # If we delete chroma files while a live ChromaDB client holds open handles, sqlite
    # shifts into readonly mode and all subsequent writes fail with code 1032.
    if clean:
        import shutil
        chroma_dir = cfg.vector_db.persist_directory or str(cfg.data_dir / "chroma")
        chroma_path = Path(chroma_dir)
        if chroma_path.exists() and cfg.vector_db.backend == "chroma":
            shutil.rmtree(chroma_path)
            chroma_path.mkdir(parents=True, exist_ok=True)

    from mimir.container import Container
    container = Container(cfg)

    try:
        if clean:
            # Wipe only the SQLite graph (chroma already wiped above)
            container.graph_store.clear()
            container._graph = None
            console.print("[dim]Wiped graph + vector store.[/]")
            graph = asyncio.run(container.indexing.index_all(mode_override=mode))
            stats = graph.stats()
            console.print(f"[green]✓ Clean full index complete[/]")
            console.print(f"  Nodes: {stats['total_nodes']}")
            console.print(f"  Edges: {stats['total_edges']}")
            console.print(f"  Repos: {', '.join(stats['repos'])}")
        else:
            graph, report = asyncio.run(container.indexing.index_incremental(mode_override=mode))
            if report.get("mode") == "full_fallback":
                stats = graph.stats()
                console.print(f"[green]✓ Initial full index complete[/]")
                console.print(f"  Nodes: {stats['total_nodes']}")
                console.print(f"  Edges: {stats['total_edges']}")
                console.print(f"  Repos: {', '.join(stats['repos'])}")
            else:
                _display_incremental_report(report)
    except Exception as exc:
        console.print(f"[red bold]Indexing failed:[/] {exc}")
        raise typer.Exit(1) from exc
    finally:
        container.close()


def _display_incremental_report(report: dict) -> None:
    """Pretty-print the incremental indexing report."""

    console.print("[green]✓ Incremental index complete[/]")

    for repo_name, info in report.get("repos", {}).items():
        status = info.get("status", "unknown")

        if status == "up_to_date":
            console.print(f"  [dim]{repo_name}:[/] [green]up to date[/] ({info.get('commit', '')})")
        elif status == "updated":
            console.print(f"  [cyan]{repo_name}:[/] [yellow]updated[/] ({info.get('commit', '')})")
            console.print(f"    Files: +{info.get('files_added', 0)} added, ~{info.get('files_modified', 0)} modified, -{info.get('files_deleted', 0)} deleted")
            console.print(f"    Parsed: {info.get('files_parsed', 0)} files, {info.get('symbols_parsed', 0)} symbols")
            console.print(f"    Nodes: -{info.get('nodes_removed', 0)} removed")
        elif status == "full_index":
            console.print(f"  [cyan]{repo_name}:[/] [yellow]full index[/] ({info.get('reason', '')})")
        elif status == "skipped":
            console.print(f"  [dim]{repo_name}:[/] [red]skipped[/] ({info.get('reason', '')})")

    console.print(f"\n  [bold]Total:[/] -{report.get('total_removed', 0)} removed, +{report.get('total_added', 0)} added")
    console.print(f"  [bold]Graph:[/] {report.get('graph_nodes', 0)} nodes, {report.get('graph_edges', 0)} edges")



# ------------------------------------------------------------------
# search
# ------------------------------------------------------------------

@app.command()
def search(
    query: str = typer.Argument(..., help="Natural language query"),
    budget: int = typer.Option(8000, "--budget", "-b", help="Token budget"),
    repos: Optional[str] = typer.Option(None, "--repos", "-r", help="Comma-separated repo filter"),
    flat: bool = typer.Option(False, "--flat", help="Force flat search"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
    config: Path = typer.Option(_DEFAULT_CONFIG, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Search the indexed codebase and assemble context."""
    _setup_logging(verbose)

    cfg, _ = _load_config(config, workspace)
    from mimir.container import Container
    container = Container(cfg)

    try:
        graph = container.load_graph()
        repo_list = repos.split(",") if repos else None

        bundle = asyncio.run(container.retrieval.search(
            query=query,
            graph=graph,
            token_budget=budget,
            repos=repo_list,
            flat=flat,
        ))

        # Display results
        console.print(f"\n[bold]{bundle.summary}[/]")
        if bundle.session_note:
            console.print(f"[dim]{bundle.session_note}[/]")
        console.print(f"[dim]Tokens: {bundle.token_count}[/]\n")

        console.print(bundle.format_for_llm())
    except Exception as exc:
        console.print(f"[red bold]Search failed:[/] {exc}")
        raise typer.Exit(1) from exc
    finally:
        container.close()



# ------------------------------------------------------------------
# graph
# ------------------------------------------------------------------

@app.command("graph")
def graph_cmd(
    stats: bool = typer.Option(False, "--stats", help="Show graph statistics"),
    show: Optional[str] = typer.Option(None, "--show", help="Show node details"),
    cross_repo: bool = typer.Option(False, "--cross-repo", help="Show cross-repo edges"),
    path_from: Optional[str] = typer.Option(None, "--path-from", help="Find path from"),
    path_to: Optional[str] = typer.Option(None, "--path-to", help="Find path to"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
    config: Path = typer.Option(_DEFAULT_CONFIG, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Explore the code graph."""
    _setup_logging(verbose)

    cfg, _ = _load_config(config, workspace)
    from mimir.container import Container
    container = Container(cfg)

    try:
        graph = container.load_graph()

        if stats:
            s = graph.stats()
            table = Table(title="Graph Statistics")
            table.add_column("Metric", style="cyan")
            table.add_column("Value", style="green")
            table.add_row("Total Nodes", str(s["total_nodes"]))
            table.add_row("Total Edges", str(s["total_edges"]))
            table.add_row("Repos", ", ".join(s["repos"]))
            for kind, count in sorted(s["nodes_by_kind"].items()):
                table.add_row(f"  {kind}", str(count))
            console.print(table)

        elif show:
            node = graph.get_node(show)
            if not node:
                console.print(f"[red]Node not found: {show}[/]")
                raise typer.Exit(1)
            console.print(f"[bold]{node.id}[/]")
            console.print(f"  Kind: {node.kind.value}")
            console.print(f"  Repo: {node.repo}")
            console.print(f"  Path: {node.path}")
            if node.signature:
                console.print(f"  Signature: {node.signature}")
            if node.summary:
                console.print(f"  Summary: {node.summary[:200]}")
            out_edges = graph.get_outgoing_edges(show)
            if out_edges:
                console.print(f"\n  Outgoing ({len(out_edges)}):")
                for e in out_edges[:20]:
                    console.print(f"    → {e.kind.value} → {e.target}")
            in_edges = graph.get_incoming_edges(show)
            if in_edges:
                console.print(f"\n  Incoming ({len(in_edges)}):")
                for e in in_edges[:20]:
                    console.print(f"    ← {e.kind.value} ← {e.source}")

        elif cross_repo:
            edges = graph.cross_repo_edges()
            table = Table(title=f"Cross-Repo Edges ({len(edges)})")
            table.add_column("Source", style="cyan")
            table.add_column("Kind", style="yellow")
            table.add_column("Target", style="green")
            for e in edges:
                table.add_row(e.source, e.kind.value, e.target)
            console.print(table)

        elif path_from and path_to:
            edges = graph.shortest_path(path_from, path_to)
            if not edges:
                console.print("[yellow]No path found[/]")
            else:
                console.print(f"[bold]Path ({len(edges)} hops):[/]")
                for e in edges:
                    console.print(f"  {e.source} --{e.kind.value}--> {e.target}")
        else:
            console.print("[yellow]Use --stats, --show, --cross-repo, or --path-from/--path-to[/]")

    finally:
        container.close()


# ------------------------------------------------------------------
# hotspots
# ------------------------------------------------------------------

@app.command()
def hotspots(
    top: int = typer.Option(20, "--top", "-n", help="Number of hotspots"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
    config: Path = typer.Option(_DEFAULT_CONFIG, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Show recently and frequently changed code."""
    _setup_logging(verbose)

    cfg, _ = _load_config(config, workspace)
    from mimir.container import Container
    container = Container(cfg)

    try:
        graph = container.load_graph()
        results = container.temporal.get_hotspots(graph, top_n=top)

        table = Table(title=f"Top {top} Hotspots")
        table.add_column("Node", style="cyan")
        table.add_column("Score", style="green")
        table.add_column("Changes", style="yellow")
        for node, score in results:
            table.add_row(node.id, f"{score:.3f}", str(node.modification_count))
        console.print(table)
    finally:
        container.close()



# ------------------------------------------------------------------
# serve (MCP)
# ------------------------------------------------------------------

@app.command()
def serve(
    http: bool = typer.Option(False, "--http", help="Start as shared HTTP server (for team access)"),
    http_port: int = typer.Option(8421, "--http-port", help="Port for the HTTP server (only with --http)"),
    http_host: str = typer.Option("0.0.0.0", "--http-host", help="Host to bind the HTTP server to"),
    remote: Optional[str] = typer.Option(None, "--remote", "-r", help="URL of a remote Mimir HTTP server to proxy (e.g. http://team-server:8421)"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
    config: Path = typer.Option(_DEFAULT_CONFIG, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Start the MCP server.

    Three modes:

    \b
      mimir serve                        stdio MCP (default, for local IDE)
      mimir serve --http                 shared HTTP server (team runs this)
      mimir serve --remote <URL>         proxy to a remote shared server (IDE connects here)
    """
    # Validate mutual exclusivity
    if http and remote:
        console.print("[red bold]Error:[/] --http and --remote are mutually exclusive.")
        raise typer.Exit(1)

    # Force logging to stderr so stdout is strictly for JSON-RPC
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    err_console = Console(stderr=True)

    if remote:
        # Remote proxy mode — no config/workspace needed
        err_console.print(f"[green]Connecting to remote Mimir server at {remote}[/]")
        from mimir.adapters.remote_mcp import run_remote_mcp
        run_remote_mcp(remote)
    elif http:
        # Shared HTTP server mode
        cfg, ws_name = _load_config(config, workspace)
        err_console.print(
            f"[green]Starting shared Mimir HTTP server on http://{http_host}:{http_port}[/]",
        )
        err_console.print(
            f"[dim]Mobile/frontend devs connect with: mimir serve --remote http://<this-host>:{http_port}[/]",
        )
        from mimir.adapters.http_server import run_http_server
        run_http_server(cfg, host=http_host, port=http_port, workspace_name=ws_name)
    else:
        # Default stdio MCP mode
        cfg, ws_name = _load_config(config, workspace)
        from mimir.adapters.mcp_server import run_mcp_server
        run_mcp_server(cfg, workspace_name=ws_name)


# ------------------------------------------------------------------
# ui
# ------------------------------------------------------------------

@app.command()
def ui(
    port: int = typer.Option(8420, "--port", "-p", help="HTTP port"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
    config: Path = typer.Option(_DEFAULT_CONFIG, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Launch the web inspector UI."""
    _setup_logging(verbose)

    cfg, _ = _load_config(config, workspace)
    from mimir.adapters.web.server import run_web_server
    console.print(f"[green]Starting Mimir Inspector at http://localhost:{port}[/]")
    run_web_server(cfg, port=port)


# ------------------------------------------------------------------
# clear
# ------------------------------------------------------------------

@app.command()
def clear(
    graph: bool = typer.Option(True, help="Clear the code graph and embeddings"),
    sessions: bool = typer.Option(True, help="Clear all conversation sessions"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
    config: Path = typer.Option(_DEFAULT_CONFIG, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Delete locally stored index data."""
    _setup_logging(verbose)

    targets: list[str] = []
    if graph:
        targets.append("code graph + embeddings")
    if sessions:
        targets.append("sessions")

    if not targets:
        console.print("[yellow]Nothing to clear — use --graph and/or --sessions[/]")
        raise typer.Exit(0)

    if not yes:
        console.print(f"[red bold]This will permanently delete:[/] {', '.join(targets)}")
        confirmed = typer.confirm("Continue?")
        if not confirmed:
            console.print("[yellow]Aborted[/]")
            raise typer.Exit(0)

    cfg, _ = _load_config(config, workspace)
    from mimir.container import Container
    container = Container(cfg)

    try:
        result = container.clear_data(graph=graph, sessions=sessions)
        console.print(f"[green]✓ Cleared:[/] {', '.join(result['cleared'])}")
    except Exception as exc:
        console.print(f"[red bold]Clear failed:[/] {exc}")
        raise typer.Exit(1) from exc
    finally:
        container.close()



# ------------------------------------------------------------------
# vacuum
# ------------------------------------------------------------------

@app.command()
def vacuum(
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
    config: Path = typer.Option(_DEFAULT_CONFIG, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Compact the SQLite graph database to reclaim unused file space."""
    _setup_logging(verbose)
    cfg, _ = _load_config(config, workspace)
    from mimir.container import Container
    container = Container(cfg)

    try:
        container.graph_store.vacuum()
        console.print("[green]✓ Database vacuumed successfully[/]")
    except Exception as exc:
        console.print(f"[red bold]Vacuum failed:[/] {exc}")
        raise typer.Exit(1) from exc
    finally:
        container.close()


# ------------------------------------------------------------------
# ask
# ------------------------------------------------------------------

@app.command()
def ask(
    query: str = typer.Argument(..., help="Natural language query"),
    budget: int = typer.Option(8000, "--budget", "-b", help="Token budget"),
    repos: Optional[str] = typer.Option(None, "--repos", "-r", help="Comma-separated repo filter"),
    flat: bool = typer.Option(False, "--flat", help="Force flat search"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
    config: Path = typer.Option(_DEFAULT_CONFIG, "--config", "-c", help="Config file path"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Interactive semantic search CLI — retrieves context and answers via LLM."""
    _setup_logging(verbose)

    cfg, _ = _load_config(config, workspace)
    from mimir.container import Container
    container = Container(cfg)
    
    if not container.llm_client:
         console.print("[red bold]Error:[/] LLM Client not configured. Add an [llm] section to mimir.toml.")
         raise typer.Exit(1)
         
    try:
        repo_list = [r.strip() for r in repos.split(",")] if repos else None
        graph = container.load_graph()
        
        bundle = asyncio.run(container.retrieval.search(
            query=query,
            graph=graph,
            token_budget=budget,
            beam_width=cfg.retrieval.default_beam_width,
            repos=repo_list,
            flat=flat,
        ))

        context = bundle.format_for_llm()
        prompt = f"Context:\n{context}\n\nQuestion:\n{query}"
        
        from rich.markdown import Markdown
        from rich.panel import Panel
        
        console.print(Panel(
            f"Assembling context from [cyan]{len(bundle.nodes)} nodes[/] across [cyan]{len(bundle.repos_involved)} repos[/]...",
            title="Mimir Semantic Search",
            border_style="blue",
        ))
        
        response = asyncio.run(container.llm_client.complete(prompt))
        
        console.print("\n")
        console.print(Markdown(response))
        console.print("\n")
        console.print(f"[dim]Sources referenced: {', '.join({n.repo + '/' + (n.path or '') for n in bundle.nodes})}[/]")

    except Exception as exc:
        console.print(f"[red bold]Search failed:[/] {exc}")
        raise typer.Exit(1) from exc
    finally:
        container.close()


# ------------------------------------------------------------------
# init
# ------------------------------------------------------------------

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
backend = "numpy"

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


if __name__ == "__main__":
    app()
