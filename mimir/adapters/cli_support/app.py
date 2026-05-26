"""CLI application bootstrap."""

from __future__ import annotations

import typer

from mimir.adapters.cli_support.commands.guardrail import guardrail_app
from mimir.adapters.cli_support.commands.indexing import register as register_indexing_commands
from mimir.adapters.cli_support.commands.runtime import register as register_runtime_commands
from mimir.adapters.cli_support.commands.workspace import workspace_app

app = typer.Typer(
    name="mimir",
    help="Mimir — Context Server v1 — Context Engine for Large-Scale Codebases",
    no_args_is_help=True,
)

app.add_typer(workspace_app, name="workspace")
app.add_typer(guardrail_app, name="guardrail")

register_indexing_commands(app)
register_runtime_commands(app)
