"""Indexer runtime CLI commands."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import typer

from mimir.adapters.cli_support.common import DEFAULT_CONFIG, console, load_config, setup_logging
from mimir.domain.index_state import IndexJobKind
from mimir.runtime import RuntimeFactory

indexer_app = typer.Typer(
    name="indexer",
    help="Build, publish, and maintain central Mimir indexes.",
    no_args_is_help=True,
)


@indexer_app.command("run")
def run_index(
    mode: Optional[str] = typer.Option(None, help="Summary mode: none, heuristic"),
    clean: bool = typer.Option(False, "--clean", help="Force a full re-index"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c", help="Config file path"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Build and publish an index version."""
    setup_logging(verbose)
    cfg, _ = load_config(config, workspace)
    runtime = RuntimeFactory(cfg).indexer()
    try:
        version = asyncio.run(runtime.run_index(clean=clean, mode_override=mode))
        console.print(f"[green]Published index version:[/] {version.version}")
        console.print(f"  Nodes: {version.node_count}")
        console.print(f"  Edges: {version.edge_count}")
        console.print(f"  Graph: {version.graph_path}")
    except Exception as exc:
        console.print(f"[red bold]Index failed:[/] {exc}")
        raise typer.Exit(1) from exc
    finally:
        runtime.close()


@indexer_app.command("sync")
def sync_repo(
    repo: str = typer.Argument(..., help="Configured repo name to sync and re-index"),
    commit_sha: Optional[str] = typer.Option(None, "--commit-sha", help="Specific commit to check out"),
    mode: Optional[str] = typer.Option(None, help="Summary mode: none, heuristic"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c", help="Config file path"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Mirror one repo, refresh it in the graph, and publish a new index version."""
    setup_logging(verbose)
    cfg, _ = load_config(config, workspace)
    runtime = RuntimeFactory(cfg).indexer()
    try:
        version = asyncio.run(runtime.sync_repo(repo, commit_sha=commit_sha, mode_override=mode))
        console.print(f"[green]Published index version:[/] {version.version}")
        console.print(f"  Repo: {repo}")
        console.print(f"  Graph: {version.graph_path}")
    except Exception as exc:
        console.print(f"[red bold]Sync failed:[/] {exc}")
        raise typer.Exit(1) from exc
    finally:
        runtime.close()


@indexer_app.command("versions")
def versions(
    limit: int = typer.Option(20, "--limit", help="Maximum versions to show"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c", help="Config file path"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """List published index versions."""
    setup_logging(verbose)
    cfg, _ = load_config(config, workspace)
    runtime = RuntimeFactory(cfg).indexer()
    try:
        for version in runtime.list_versions(limit=limit):
            marker = "*" if version.active else " "
            console.print(
                f"{marker} {version.version} "
                f"nodes={version.node_count} edges={version.edge_count} "
                f"model={version.embedding_model or '-'} dim={version.embedding_dim or '-'}"
            )
    finally:
        runtime.close()


@indexer_app.command("activate")
def activate(
    version: str = typer.Argument(..., help="Published index version to activate"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c", help="Config file path"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Activate a previously published index version."""
    setup_logging(verbose)
    cfg, _ = load_config(config, workspace)
    runtime = RuntimeFactory(cfg).indexer()
    try:
        activated = runtime.activate_version(version)
        console.print(f"[green]Activated index version:[/] {activated.version}")
    except Exception as exc:
        console.print(f"[red bold]Activate failed:[/] {exc}")
        raise typer.Exit(1) from exc
    finally:
        runtime.close()


@indexer_app.command("prune")
def prune(
    keep: int = typer.Option(5, "--keep", help="Newest versions to retain, active is always retained"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c", help="Config file path"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Delete old inactive published index versions."""
    setup_logging(verbose)
    cfg, _ = load_config(config, workspace)
    runtime = RuntimeFactory(cfg).indexer()
    try:
        removed = runtime.prune_versions(keep=keep)
        if removed:
            console.print(f"[green]Pruned {len(removed)} index version(s)[/]")
            for version in removed:
                console.print(f"  {version}")
        else:
            console.print("[yellow]No index versions pruned[/]")
    except Exception as exc:
        console.print(f"[red bold]Prune failed:[/] {exc}")
        raise typer.Exit(1) from exc
    finally:
        runtime.close()


@indexer_app.command("enqueue")
def enqueue(
    kind: IndexJobKind = typer.Argument(..., help="Job kind"),
    repo: Optional[str] = typer.Option(None, "--repo", help="Repo name for sync jobs"),
    commit_sha: Optional[str] = typer.Option(None, "--commit-sha", help="Specific commit for sync jobs"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c", help="Config file path"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Create a durable indexer job."""
    setup_logging(verbose)
    if kind is IndexJobKind.SYNC and not repo:
        console.print("[red bold]Error:[/] sync jobs require --repo")
        raise typer.Exit(1)
    cfg, _ = load_config(config, workspace)
    runtime = RuntimeFactory(cfg).indexer()
    try:
        job = runtime.enqueue(kind, repo=repo, commit_sha=commit_sha)
        console.print(f"[green]Queued index job:[/] {job.id}")
        console.print(f"  Kind: {job.kind.value}")
        console.print(f"  Status: {job.status.value}")
    finally:
        runtime.close()


@indexer_app.command("worker")
def worker(
    once: bool = typer.Option(False, "--once", help="Process at most one queued job and exit"),
    poll_interval: float = typer.Option(5.0, "--poll-interval", help="Idle polling interval in seconds"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c", help="Config file path"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the durable indexer worker."""
    setup_logging(verbose)
    cfg, _ = load_config(config, workspace)
    runtime = RuntimeFactory(cfg).indexer()
    try:
        if once:
            job = asyncio.run(runtime.worker_once())
            if job is None:
                console.print("[yellow]No queued jobs[/]")
            else:
                console.print(f"[green]Processed job:[/] {job.id} ({job.status.value})")
        else:
            console.print("[green]Indexer worker running[/]")
            asyncio.run(runtime.worker_loop(poll_interval=poll_interval))
    except KeyboardInterrupt:
        console.print("[yellow]Worker stopped[/]")
    finally:
        runtime.close()
