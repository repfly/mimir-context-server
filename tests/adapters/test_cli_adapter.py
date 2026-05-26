from __future__ import annotations

from typer.testing import CliRunner

from mimir.adapters.cli import app as shim_app
from mimir.adapters.cli_support import app as package_app

runner = CliRunner()


def test_cli_shim_exports_same_app_instance() -> None:
    assert shim_app is package_app


def test_root_help_lists_expected_commands() -> None:
    result = runner.invoke(shim_app, ["--help"])
    assert result.exit_code == 0
    for command in ("init", "workspace", "guardrail", "indexer", "query"):
        assert command in result.stdout


def test_indexer_and_query_help_render() -> None:
    indexer = runner.invoke(shim_app, ["indexer", "--help"])
    query = runner.invoke(shim_app, ["query", "--help"])

    assert indexer.exit_code == 0
    assert "run" in indexer.stdout
    assert "sync" in indexer.stdout
    assert "worker" in indexer.stdout
    assert query.exit_code == 0
    assert "search" in query.stdout
    assert "serve" in query.stdout
    assert "status" in query.stdout
    assert "ui" in query.stdout


def test_workspace_help_renders() -> None:
    result = runner.invoke(shim_app, ["workspace", "--help"])
    assert result.exit_code == 0
    assert "add" in result.stdout
    assert "list" in result.stdout
    assert "remove" in result.stdout


def test_guardrail_help_renders() -> None:
    result = runner.invoke(shim_app, ["guardrail", "--help"])
    assert result.exit_code == 0
    assert "check" in result.stdout
    assert "init" in result.stdout
    assert "test" in result.stdout
    assert "approve" in result.stdout
