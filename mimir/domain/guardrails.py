"""Domain models for the architectural guardrails system.

Defines the core types: rules, violations, change sets, and evaluation results.
All dataclasses are frozen to enforce immutability in the domain layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, unique
from typing import Any

from mimir.domain.models import Edge, Node


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

@unique
class RuleType(Enum):
    """Types of architectural rules that can be enforced."""

    DEPENDENCY_BAN = "dependency_ban"
    CYCLE_DETECTION = "cycle_detection"
    METRIC_THRESHOLD = "metric_threshold"
    IMPACT_THRESHOLD = "impact_threshold"
    FILE_SCOPE_BAN = "file_scope_ban"


@unique
class Severity(Enum):
    """Severity of a rule violation."""

    WARNING = "warning"    # report but don't block
    ERROR = "error"        # block commit / fail CI
    BLOCK = "block"        # block + require human approval


# ---------------------------------------------------------------------------
# Rule
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rule:
    """A single architectural constraint."""

    id: str
    type: RuleType
    description: str
    severity: Severity
    config: dict[str, Any] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Rule id must not be empty")


# ---------------------------------------------------------------------------
# ChangeSet
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChangeSet:
    """Represents what a diff actually changed in terms of the code graph."""

    modified_nodes: tuple[str, ...] = ()       # IDs of existing nodes with changed code
    new_nodes: tuple[Node, ...] = ()           # nodes introduced by the change
    new_edges: tuple[Edge, ...] = ()           # new dependency edges introduced
    removed_edges: tuple[Edge, ...] = ()       # dependency edges removed
    affected_files: tuple[str, ...] = ()       # file paths touched by the diff


# ---------------------------------------------------------------------------
# Violation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Violation:
    """A rule that was violated by the change."""

    rule_id: str
    rule_description: str
    severity: Severity
    message: str
    evidence: tuple[str, ...] = ()
    file_path: str | None = None
    suggested_fix: str | None = None
    approval_status: str | None = None  # "approved", "pending", or None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "rule_id": self.rule_id,
            "rule_description": self.rule_description,
            "severity": self.severity.value,
            "message": self.message,
            "evidence": list(self.evidence),
            "file_path": self.file_path,
            "suggested_fix": self.suggested_fix,
        }
        if self.approval_status is not None:
            d["approval_status"] = self.approval_status
        return d


# ---------------------------------------------------------------------------
# GuardrailResult
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GuardrailResult:
    """Complete result of evaluating a change against all rules."""

    violations: tuple[Violation, ...]
    passed: bool
    summary: str
    change_set: ChangeSet
    rules_evaluated: int
    pending_approvals: tuple[str, ...] = ()  # rule_ids with unresolved BLOCK

    @property
    def has_pending_blocks(self) -> bool:
        """True when there are BLOCK violations awaiting human approval."""
        return len(self.pending_approvals) > 0

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "passed": self.passed,
            "summary": self.summary,
            "rules_evaluated": self.rules_evaluated,
            "violations": [v.to_dict() for v in self.violations],
            "affected_files": list(self.change_set.affected_files),
            "modified_nodes": list(self.change_set.modified_nodes),
        }
        if self.pending_approvals:
            d["pending_approvals"] = list(self.pending_approvals)
        return d

    def format_for_llm(self) -> str:
        """Render as structured text for LLM consumption."""
        parts: list[str] = []
        status = "PASSED" if self.passed else "FAILED"
        parts.append(f"# Guardrail Check: {status}")
        parts.append(f"Rules evaluated: {self.rules_evaluated}")
        parts.append(f"Violations: {len(self.violations)}")
        parts.append("")

        if not self.violations:
            parts.append("No architectural violations detected.")
            return "\n".join(parts)

        parts.append("## Violations")
        parts.append("")
        for v in self.violations:
            sev_label = v.severity.value.upper()
            if v.approval_status == "approved":
                sev_label = "BLOCK - APPROVED"
            elif v.approval_status == "pending":
                sev_label = "BLOCK - PENDING"
            parts.append(f"### [{sev_label}] {v.rule_id}")
            parts.append(f"**Rule:** {v.rule_description}")
            parts.append(f"**Details:** {v.message}")
            if v.file_path:
                parts.append(f"**File:** {v.file_path}")
            if v.evidence:
                parts.append("**Evidence:**")
                for e in v.evidence:
                    parts.append(f"  - {e}")
            if v.suggested_fix:
                parts.append(f"**Suggested fix:** {v.suggested_fix}")
            parts.append("")

        return "\n".join(parts)
