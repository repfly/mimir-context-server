"""Guardrail-related CLI commands."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer

from mimir.adapters.cli_support.common import DEFAULT_CONFIG, console, load_config, setup_logging

guardrail_app = typer.Typer(
    name="guardrail",
    help="Architectural guardrails — validate changes against structural rules.",
    no_args_is_help=True,
)


def git_auto_diff(base: str = "") -> str:
    """Auto-detect a diff from git."""

    def run_git(*args: str) -> str:
        try:
            return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL)
        except Exception:
            return ""

    staged = run_git("diff", "--cached")
    if staged.strip():
        console.print("[dim]Using staged changes (git diff --cached)[/dim]")
        return staged

    unstaged = run_git("diff")
    if unstaged.strip():
        console.print("[dim]Using unstaged changes (git diff)[/dim]")
        return unstaged

    if not base:
        for candidate in ("main", "master", "develop"):
            if run_git("rev-parse", "--verify", f"refs/heads/{candidate}").strip():
                base = candidate
                break
        if not base:
            base = "HEAD~1"

    branch_diff = run_git("diff", f"{base}...HEAD")
    if branch_diff.strip():
        console.print(f"[dim]Using branch diff ({base}...HEAD)[/dim]")
        return branch_diff
    return ""


@guardrail_app.command("check")
def guardrail_check(
    diff: str = typer.Option("", "--diff", "-d", help="Path to diff file, '-' for stdin, or empty for auto-detect from git"),
    base: str = typer.Option("", "--base", "-b", help="Base ref for git diff (e.g. main, origin/main). Default: auto-detect"),
    rules: Path = typer.Option(Path("mimir-rules.yaml"), "--rules", "-r", help="Path to rules YAML file"),
    output: str = typer.Option("text", "--output", "-o", help="Output format: text, json, github-pr-comment"),
    report_file: Optional[Path] = typer.Option(None, "--report-file", help="Write the formatted report to a file (in addition to stdout)"),
    no_approvals: bool = typer.Option(False, "--no-approvals", help="Ignore HEAD commit trailers (show raw BLOCK violations)"),
    head: str = typer.Option(
        "HEAD", "--head",
        help="Git ref whose commit message is scanned for Mimir-Approved trailers.",
    ),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Validate a diff against architectural rules."""
    setup_logging(verbose)

    from mimir.domain.guardrails_config import load_rules
    from mimir.services.guardrail import apply_approvals
    from mimir.services.guardrail.report import GuardrailReporter
    from mimir.services.guardrail.trailers import read_head_approval
    from mimir.container import Container

    try:
        rule_list = load_rules(rules)
    except Exception as exc:
        console.print(f"[red bold]Rule loading error:[/] {exc}")
        raise typer.Exit(1) from exc

    if diff == "-":
        diff_text = sys.stdin.read()
    elif diff:
        diff_path = Path(diff)
        if not diff_path.exists():
            console.print(f"[red bold]Diff file not found:[/] {diff}")
            raise typer.Exit(1)
        diff_text = diff_path.read_text()
    else:
        diff_text = git_auto_diff(base)

    if not diff_text.strip():
        console.print("[yellow]Empty diff — nothing to check.[/]")
        raise typer.Exit(0)

    cfg, _ = load_config(config, workspace)
    container = Container(cfg)
    try:
        graph = container.load_graph()
        result = asyncio.run(container.guardrail.evaluate(graph, diff_text, rule_list))

        if not no_approvals and any(v.severity.value == "block" for v in result.violations):
            head_approval = read_head_approval(head)
            result = apply_approvals(
                result,
                approved_rule_ids=head_approval.rule_ids if head_approval else frozenset(),
                reason=head_approval.reason if head_approval else "",
            )

        reporter = GuardrailReporter()
        if output == "json":
            formatted = json.dumps(result.to_dict(), indent=2)
            console.print_json(formatted)
        elif output == "github-pr-comment":
            formatted = reporter.format_github_pr_comment(result)
            console.print(formatted)
        else:
            formatted = reporter.format_text(result)
            console.print(formatted)

        if report_file:
            report_file.parent.mkdir(parents=True, exist_ok=True)
            report_file.write_text(formatted + "\n", encoding="utf-8")

        if not result.passed:
            raise typer.Exit(1)
    finally:
        container.close()


@guardrail_app.command("init")
def guardrail_init(
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing files"),
) -> None:
    """Generate example mimir-rules.yaml and mimir-agent-policy.yaml."""
    import shutil

    examples = {
        "mimir-rules.yaml": Path(__file__).parent.parent.parent.parent.parent / "mimir-rules.yaml",
        "mimir-agent-policy.yaml": Path(__file__).parent.parent.parent.parent.parent / "mimir-agent-policy.yaml",
    }
    for name, source in examples.items():
        target = Path(name)
        if target.exists() and not force:
            console.print(f"[yellow]Skipping {name} (already exists, use --force to overwrite)[/]")
            continue
        if source.exists():
            shutil.copy2(source, target)
            console.print(f"[green]Created {name}[/]")
        else:
            console.print(f"[yellow]Template {name} not found in package[/]")

    console.print("\n[bold]Next steps:[/]")
    console.print("  1. Edit mimir-rules.yaml to match your architecture")
    console.print("  2. Run: git diff | mimir guardrail check --diff -")


@guardrail_app.command("test")
def guardrail_test(
    rules: Path = typer.Option(Path("mimir-rules.yaml"), "--rules", "-r", help="Path to rules YAML file"),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Dry-run: validate rule syntax and report current metric values."""
    setup_logging(verbose)

    from mimir.domain.guardrails_config import load_rules
    from mimir.container import Container

    try:
        rule_list = load_rules(rules)
    except Exception as exc:
        console.print(f"[red bold]Rule loading error:[/] {exc}")
        raise typer.Exit(1) from exc

    console.print(f"[green]Loaded {len(rule_list)} rules from {rules}[/]")
    for rule in rule_list:
        console.print(f"  [{rule.severity.value}] {rule.id}: {rule.description}")

    try:
        cfg, _ = load_config(config, workspace)
    except Exception:
        console.print("\n[yellow]No config found — skipping graph analysis.[/]")
        return

    container = Container(cfg)
    try:
        graph = container.load_graph()
        console.print(f"\n[bold]Graph:[/] {graph.node_count} nodes, {graph.edge_count} edges")
        console.print("[green]Rules syntax OK. Ready for guardrail checks.[/]")
    finally:
        container.close()


@guardrail_app.command("approve")
def guardrail_approve(
    rule_ids: list[str] = typer.Argument(..., help="Rule IDs to approve (space-separated)"),
    reason: str = typer.Option(..., "--reason", help="Reason for approving"),
) -> None:
    """Approve BLOCK violations by committing an approval trailer on HEAD."""
    if not reason.strip():
        console.print("[red bold]--reason must not be empty[/]")
        raise typer.Exit(1)

    ids = [rule_id.strip() for rule_id in rule_ids if rule_id.strip()]
    if not ids:
        console.print("[red bold]At least one rule id is required[/]")
        raise typer.Exit(1)

    body = (
        f"approval: {', '.join(ids)}\n"
        "\n"
        f"Mimir-Approved: {', '.join(ids)}\n"
        f"Mimir-Approved-Reason: {reason.strip()}\n"
    )
    try:
        subprocess.run(["git", "commit", "--allow-empty", "-m", body], check=True)
    except subprocess.CalledProcessError as exc:
        console.print(f"[red bold]git commit failed:[/] {exc}")
        raise typer.Exit(1) from exc

    console.print(f"[green]Approval commit created for:[/] {', '.join(ids)}")
    console.print(f"  Reason: {reason}")
    console.print("")
    console.print("[bold]Don't forget to push:[/] git push")
