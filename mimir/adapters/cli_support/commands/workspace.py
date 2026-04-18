"""Workspace management CLI commands."""

from __future__ import annotations

from pathlib import Path

import typer

from mimir.adapters.cli_support.common import DEFAULT_CONFIG, console
from mimir.domain.config import ConfigError

workspace_app = typer.Typer(
    name="workspace",
    help="Manage named Mimir workspaces (~/.mimir/workspaces.toml).",
    no_args_is_help=True,
)


@workspace_app.command("add")
def workspace_add(
    name: str = typer.Argument(..., help="Workspace name (letters, digits, hyphens, underscores)"),
    config: Path = typer.Option(
        DEFAULT_CONFIG, "--config", "-c",
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
    from rich.table import Table
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
        table.add_row(name, str(path), "✓" if path.is_file() else "✗ missing")
    console.print(table)


@workspace_app.command("remove")
def workspace_remove(
    name: str = typer.Argument(..., help="Workspace name to remove"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Unregister a workspace from the global registry."""
    from mimir.domain.workspace import WorkspaceRegistry

    registry = WorkspaceRegistry()
    if not yes and not typer.confirm(f"Remove workspace '{name}' from registry?"):
        console.print("[yellow]Aborted[/]")
        raise typer.Exit(0)
    try:
        registry.remove(name)
        console.print(f"[green]✓ Removed workspace:[/] {name}")
    except ConfigError as exc:
        console.print(f"[red bold]Error:[/] {exc}")
        raise typer.Exit(1) from exc
