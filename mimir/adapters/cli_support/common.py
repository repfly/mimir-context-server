"""Shared CLI adapter helpers."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from mimir.domain.config import ConfigError, MimirConfig

console = Console()
stderr_console = Console(stderr=True)

DEFAULT_CONFIG = Path("mimir.toml")


def resolve_config_path(
    config: Path,
    workspace: Optional[str] = None,
) -> tuple[Path, Optional[str]]:
    """Resolve the config path and workspace name, enforcing mutual exclusivity."""
    resolved_workspace = workspace or os.environ.get("MIMIR_WORKSPACE") or None
    explicit_config = config != DEFAULT_CONFIG

    if resolved_workspace and explicit_config:
        stderr_console.print(
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
            stderr_console.print(f"[red bold]Workspace error:[/] {exc}")
            raise typer.Exit(1) from exc
        return config_path, resolved_workspace

    return config, None


def load_config(
    config: Path,
    workspace: Optional[str] = None,
) -> tuple[MimirConfig, Optional[str]]:
    """Load and validate config with user-friendly error messages."""
    config_path, workspace_name = resolve_config_path(config, workspace)
    try:
        return MimirConfig.load(config_path), workspace_name
    except ConfigError as exc:
        stderr_console.print(f"[red bold]Config error:[/] {exc}")
        raise typer.Exit(1) from exc


def enable_watcher(watcher_config):
    """Return a copy of WatcherConfig with enabled=True."""
    from dataclasses import replace

    return replace(watcher_config, enabled=True)


def setup_logging(verbose: bool) -> None:
    """Configure Rich logging for standard CLI commands."""
    from rich.logging import RichHandler

    level = logging.DEBUG if verbose else logging.INFO
    handler = RichHandler(rich_tracebacks=True, console=console, markup=False, show_path=False)
    handler.setFormatter(logging.Formatter("%(name)s — %(message)s", datefmt="[%X]"))
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)
    logging.getLogger("aiohttp.access").setLevel(logging.ERROR)


def setup_stdio_logging(verbose: bool) -> None:
    """Force logging to stderr so stdout is reserved for JSON-RPC."""
    import sys

    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
