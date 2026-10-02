"""CLI application bootstrap."""

from __future__ import annotations

from pathlib import Path

import typer

from mimir.adapters.cli_support.common import console
from mimir.adapters.cli_support.commands.guardrail import guardrail_app
from mimir.adapters.cli_support.commands.indexer import indexer_app
from mimir.adapters.cli_support.commands.query import query_app
from mimir.adapters.cli_support.commands.workspace import workspace_app

app = typer.Typer(
    name="mimir",
    help="Mimir — Context Server v1 — Context Engine for Large-Scale Codebases",
    no_args_is_help=True,
)

app.add_typer(workspace_app, name="workspace")
app.add_typer(guardrail_app, name="guardrail")
app.add_typer(indexer_app, name="indexer")
app.add_typer(query_app, name="query")


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

[retrieval]
default_beam_width = 3
default_token_budget = 8000
expansion_hops = 2
'''
    config_path.write_text(template)
    console.print(f"[green]Created {config_path}[/]")
    console.print("  Edit the [[repos]] section to point to your code repositories.")
