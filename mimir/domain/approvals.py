"""Domain models for the guardrail approval workflow.

Approval tokens are git-native, platform-agnostic YAML files stored in
``.mimir/approvals/``.  They bind a human approval to a specific diff hash
so that BLOCK-severity violations can be resolved without removing the change.

All dataclasses are frozen to enforce immutability in the domain layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, unique
from typing import Any


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

@unique
class ApprovalStatus(Enum):
    """Lifecycle state of an approval request."""

    PENDING = "pending"
    APPROVED = "approved"
    REVOKED = "revoked"
    EXPIRED = "expired"


# ---------------------------------------------------------------------------
# ApprovalRequest
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ApprovalRequest:
    """A request (and optionally grant) of human approval for BLOCK violations.

    The ``diff_hash`` binds the approval to the exact diff content — any code
    change after approval invalidates the token.
    """

    id: str
    rule_ids: tuple[str, ...]
    diff_hash: str
    status: ApprovalStatus
    requested_by: str
    requested_at: str  # ISO-8601 UTC
    affected_files: tuple[str, ...] = ()
    approved_by: str | None = None
    approved_at: str | None = None
    reason: str | None = None
    expires_at: str | None = None  # ISO-8601 UTC
    revoked_by: str | None = None
    revoked_at: str | None = None

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("ApprovalRequest id must not be empty")
        if not self.rule_ids:
            raise ValueError("ApprovalRequest must reference at least one rule")

    def is_valid_for(self, rule_id: str, diff_hash: str, now: datetime | None = None) -> bool:
        """Return True if this approval covers *rule_id* for *diff_hash* and has not expired."""
        if self.status != ApprovalStatus.APPROVED:
            return False
        if rule_id not in self.rule_ids:
            return False
        if self.diff_hash != diff_hash:
            return False
        if self.expires_at:
            now = now or datetime.now(timezone.utc)
            try:
                expiry = datetime.fromisoformat(self.expires_at)
                if now >= expiry:
                    return False
            except ValueError:
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "rule_ids": list(self.rule_ids),
            "diff_hash": self.diff_hash,
            "status": self.status.value,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "affected_files": list(self.affected_files),
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "reason": self.reason,
            "expires_at": self.expires_at,
            "revoked_by": self.revoked_by,
            "revoked_at": self.revoked_at,
        }


# ---------------------------------------------------------------------------
# ApprovalConfig
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ApprovalConfig:
    """Global approval settings parsed from ``mimir-rules.yaml``."""

    default_ttl_days: int = 7
    approvers: tuple[str, ...] = ()
    approvals_dir: str = ".mimir/approvals"
