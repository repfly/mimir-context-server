from __future__ import annotations

from textwrap import dedent

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
    assert "versions" in indexer.stdout
    assert "activate" in indexer.stdout
    assert "prune" in indexer.stdout
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


def test_query_status_succeeds_without_active_index(tmp_path) -> None:
    config_path = _write_config(tmp_path)

    result = runner.invoke(shim_app, ["query", "status", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Active version: -" in result.stdout


def test_query_search_fails_without_active_index(tmp_path) -> None:
    config_path = _write_config(tmp_path)

    result = runner.invoke(shim_app, ["query", "search", "auth", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "No active index" in result.stdout
    assert "mimir indexer run" in result.stdout


def test_guardrail_check_fails_without_active_index(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(dedent("""
        rules:
          - id: no-adapters
            type: dependency_ban
            description: "Adapters are blocked"
            severity: error
            config:
              source_pattern: "*.py"
              target_pattern: "*/adapters/*"
    """))
    diff = dedent("""
        diff --git a/repo/app.py b/repo/app.py
        index 1111111..2222222 100644
        --- a/repo/app.py
        +++ b/repo/app.py
        @@ -1 +1,2 @@
         print("ok")
        +import mimir.adapters.http
    """)

    result = runner.invoke(
        shim_app,
        ["guardrail", "check", "--diff", "-", "--rules", str(rules_path), "--config", str(config_path)],
        input=diff,
    )

    assert result.exit_code == 1
    assert "No active index" in result.stdout
    assert "mimir indexer run" in result.stdout


def _write_config(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "mimir.toml"
    config_path.write_text(dedent(f"""
        data_dir = "{tmp_path / '.mimir'}"

        [[repos]]
        name = "repo"
        path = "{repo}"
        language_hint = "python"

        [embeddings]
        model = "local:test"

        [vector_db]
        backend = "numpy"
    """))
    return config_path
