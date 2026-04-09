"""Formats GuardrailResult for various output targets.

Supports terminal text (rich-compatible), JSON, GitHub PR comment markdown,
and audit log entries for compliance evidence.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from mimir.domain.guardrails import GuardrailResult, Severity


def _pending_rule_ids(result: GuardrailResult) -> list[str]:
    """Return unique rule ids for BLOCK violations still marked pending."""
    seen: list[str] = []
    for v in result.violations:
        if (
            v.severity == Severity.BLOCK
            and v.approval_status == "pending"
            and v.rule_id not in seen
        ):
            seen.append(v.rule_id)
    return seen


def _approval_instructions(pending: list[str]) -> list[str]:
    """Return the copy-paste approval command for the given pending rules."""
    rule_args = " ".join(pending)
    return [
        "Run on the PR branch to clear these BLOCKs:",
        f"  mimir guardrail approve {rule_args} --reason \"...\"",
        "  git push",
        "",
        "This creates an empty commit with a Mimir-Approved trailer on HEAD.",
    ]


class GuardrailReporter:
    """Formats GuardrailResult for different output targets."""

    def format_text(self, result: GuardrailResult) -> str:
        """Human-readable terminal output with rich markup."""
        parts: list[str] = []

        if result.passed:
            parts.append("[green bold]✓ Guardrail check PASSED[/green bold]")
        else:
            parts.append("[red bold]✗ Guardrail check FAILED[/red bold]")

        parts.append(f"  Rules evaluated: {result.rules_evaluated}")
        parts.append(f"  Files affected: {len(result.change_set.affected_files)}")
        parts.append(f"  Violations: {len(result.violations)}")

        if result.violations:
            parts.append("")
            for v in result.violations:
                if v.severity == Severity.BLOCK and v.approval_status == "approved":
                    severity_style = "[green]BLOCK (approved)[/green]"
                elif v.severity == Severity.BLOCK and v.approval_status == "pending":
                    severity_style = "[yellow bold]BLOCK (pending approval)[/yellow bold]"
                else:
                    severity_style = {
                        Severity.WARNING: "[yellow]WARNING[/yellow]",
                        Severity.ERROR: "[red]ERROR[/red]",
                        Severity.BLOCK: "[red bold]BLOCK[/red bold]",
                    }.get(v.severity, v.severity.value)

                parts.append(f"  {severity_style} [{v.rule_id}] {v.message}")
                if v.file_path:
                    parts.append(f"    File: {v.file_path}")
                if v.suggested_fix:
                    parts.append(f"    Fix: {v.suggested_fix}")

        pending = _pending_rule_ids(result)
        if pending:
            parts.append("")
            parts.append("[yellow]To resolve pending blocks:[/yellow]")
            for line in _approval_instructions(pending):
                parts.append(f"  {line}" if line else "")

        return "\n".join(parts)

    def format_json(self, result: GuardrailResult) -> dict[str, Any]:
        """Machine-readable JSON."""
        return result.to_dict()

    def format_github_pr_comment(self, result: GuardrailResult) -> str:
        """Markdown formatted for GitHub PR comment."""
        parts: list[str] = []

        if result.passed:
            parts.append("## ✅ Mimir Guardrail Check — Passed")
        else:
            parts.append("## ❌ Mimir Guardrail Check — Failed")

        parts.append("")
        parts.append(f"**Rules evaluated:** {result.rules_evaluated}")
        parts.append(f"**Files affected:** {len(result.change_set.affected_files)}")
        parts.append(f"**Violations:** {len(result.violations)}")

        if result.violations:
            parts.append("")
            parts.append("### Violations")
            parts.append("")
            parts.append("| Severity | Rule | Details | File |")
            parts.append("|----------|------|---------|------|")

            for v in result.violations:
                if v.severity == Severity.BLOCK and v.approval_status == "approved":
                    severity_badge = "✅ block (approved)"
                elif v.severity == Severity.BLOCK and v.approval_status == "pending":
                    severity_badge = "⏳ block (pending)"
                else:
                    severity_badge = {
                        Severity.WARNING: "⚠️ warning",
                        Severity.ERROR: "🔴 error",
                        Severity.BLOCK: "🚫 block",
                    }.get(v.severity, v.severity.value)

                file_str = f"`{v.file_path}`" if v.file_path else "-"
                parts.append(
                    f"| {severity_badge} | `{v.rule_id}` | {v.message} | {file_str} |"
                )

            # Suggested fixes
            fixes = [v for v in result.violations if v.suggested_fix]
            if fixes:
                parts.append("")
                parts.append("### Suggested Fixes")
                parts.append("")
                for v in fixes:
                    parts.append(f"- **{v.rule_id}**: {v.suggested_fix}")

            # Approval instructions for pending blocks
            pending = _pending_rule_ids(result)
            if pending:
                parts.append("")
                parts.append("### Approval Required")
                parts.append("")
                parts.append(
                    "These BLOCK rules need a human approval. On the PR "
                    "branch, run:"
                )
                parts.append("")
                parts.append("```bash")
                parts.append(f"mimir guardrail approve {' '.join(pending)} --reason \"...\"")
                parts.append("git push")
                parts.append("```")
                parts.append("")
                parts.append(
                    "The command adds an empty commit with a "
                    "`Mimir-Approved:` trailer on HEAD. Any subsequent commit "
                    "without the trailer re-invalidates the approval."
                )
        else:
            parts.append("")
            parts.append("No architectural violations detected. 🎉")

        parts.append("")
        parts.append("---")
        parts.append("*Generated by [Mimir Guardrails](https://github.com/repfly/mimir)*")

        return "\n".join(parts)

    def format_audit_log(
        self,
        result: GuardrailResult,
        *,
        agent_id: Optional[str] = None,
        change_hash: Optional[str] = None,
    ) -> dict[str, Any]:
        """Structured audit entry for compliance (EU AI Act Article 9)."""
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "change_hash": change_hash,
            "agent_id": agent_id,
            "rules_evaluated": result.rules_evaluated,
            "passed": result.passed,
            "violation_count": len(result.violations),
            "violations": [v.to_dict() for v in result.violations],
            "affected_files": list(result.change_set.affected_files),
        }


def append_audit_entry(data_dir: Path, entry: dict[str, Any]) -> None:
    """Append an audit log entry to guardrail_audit.jsonl."""
    audit_file = data_dir / "guardrail_audit.jsonl"
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    with audit_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
