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


class GuardrailReporter:
    """Formats GuardrailResult for different output targets."""

    def format_text(
        self, result: GuardrailResult, *,
        pending_rule_ids: tuple[str, ...] = (),
        approval_request_id: str | None = None,
    ) -> str:
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

        if pending_rule_ids:
            parts.append("")
            if approval_request_id:
                parts.append(
                    f"[yellow]Approval request created:[/yellow] {approval_request_id}"
                )
                parts.append("[yellow]To approve:[/yellow]")
                parts.append(
                    f"  mimir guardrail approve {approval_request_id} --reason \"...\""
                )
            else:
                rule_csv = ",".join(pending_rule_ids)
                parts.append("[yellow]To resolve pending blocks:[/yellow]")
                parts.append(f"  mimir guardrail request --rules {rule_csv}")
                parts.append("  mimir guardrail approve <request-id> --reason \"...\"")
            parts.append("  git add .mimir/approvals/ && git commit && git push")

        return "\n".join(parts)

    def format_json(self, result: GuardrailResult) -> dict[str, Any]:
        """Machine-readable JSON."""
        return result.to_dict()

    def format_github_pr_comment(
        self, result: GuardrailResult, *,
        pending_rule_ids: tuple[str, ...] = (),
        approval_request_id: str | None = None,
    ) -> str:
        """Markdown formatted for GitHub PR comment."""
        parts: list[str] = []

        if result.passed:
            parts.append("## ✅ Mimir Guardrail Check — Passed")
        elif result.has_pending_blocks and not any(
            v.severity == Severity.ERROR for v in result.violations
        ):
            parts.append("## ⏳ Mimir Guardrail Check — Pending Approval")
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
            if pending_rule_ids:
                parts.append("")
                parts.append("### Approval Required")
                parts.append("")
                if approval_request_id:
                    parts.append(
                        f"Approval request `{approval_request_id}` was auto-created. "
                        "Run locally to approve:"
                    )
                    parts.append("```bash")
                    parts.append(
                        f"mimir guardrail approve {approval_request_id} --reason \"...\""
                    )
                else:
                    rule_csv = ",".join(pending_rule_ids)
                    parts.append("To approve these changes, run locally:")
                    parts.append("```bash")
                    parts.append(f"mimir guardrail request --rules {rule_csv}")
                    parts.append("mimir guardrail approve <request-id> --reason \"...\"")
                parts.append("git add .mimir/approvals/ && git commit && git push")
                parts.append("```")
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
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "change_hash": change_hash,
            "agent_id": agent_id,
            "rules_evaluated": result.rules_evaluated,
            "passed": result.passed,
            "violation_count": len(result.violations),
            "violations": [v.to_dict() for v in result.violations],
            "affected_files": list(result.change_set.affected_files),
        }
        if result.pending_approvals:
            entry["pending_approvals"] = list(result.pending_approvals)
        return entry


def append_audit_entry(data_dir: Path, entry: dict[str, Any]) -> None:
    """Append an audit log entry to guardrail_audit.jsonl."""
    audit_file = data_dir / "guardrail_audit.jsonl"
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    with audit_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
