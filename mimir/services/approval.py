"""Manages approval tokens for BLOCK-severity guardrail violations.

Approval state is stored as YAML files in ``.mimir/approvals/`` so that it
lives in the repository itself — platform-agnostic and auditable via git log.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from mimir.domain.approvals import ApprovalConfig, ApprovalRequest, ApprovalStatus
from mimir.domain.errors import GuardrailError

logger = logging.getLogger(__name__)


class ApprovalService:
    """CRUD operations on approval request files."""

    def __init__(self, approvals_dir: Path) -> None:
        self._dir = approvals_dir

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_request(
        self,
        *,
        rule_ids: list[str],
        diff_text: str,
        branch: str,
        requested_by: str,
        affected_files: list[str] | None = None,
        ttl_days: int = 7,
    ) -> ApprovalRequest:
        """Create a new pending approval request and write it to disk."""
        request_id = f"apr-{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=ttl_days)

        req = ApprovalRequest(
            id=request_id,
            rule_ids=tuple(rule_ids),
            diff_hash=self.compute_diff_hash(diff_text),
            branch=branch,
            status=ApprovalStatus.PENDING,
            requested_by=requested_by,
            requested_at=now.isoformat(),
            affected_files=tuple(affected_files or []),
            expires_at=expires.isoformat(),
        )

        self._write(req)
        return req

    def approve(
        self,
        request_id: str,
        *,
        approved_by: str,
        reason: str,
        approvers_allowed: list[str] | None = None,
    ) -> ApprovalRequest:
        """Grant approval for a pending request."""
        req = self.load_request(request_id)

        if req.status != ApprovalStatus.PENDING:
            raise GuardrailError(
                f"Cannot approve request {request_id}: status is {req.status.value}"
            )

        if approvers_allowed and approved_by not in approvers_allowed:
            raise GuardrailError(
                f"User '{approved_by}' is not in the authorized approvers list"
            )

        now = datetime.now(timezone.utc)
        updated = ApprovalRequest(
            id=req.id,
            rule_ids=req.rule_ids,
            diff_hash=req.diff_hash,
            branch=req.branch,
            status=ApprovalStatus.APPROVED,
            requested_by=req.requested_by,
            requested_at=req.requested_at,
            affected_files=req.affected_files,
            approved_by=approved_by,
            approved_at=now.isoformat(),
            reason=reason,
            expires_at=req.expires_at,
        )

        self._write(updated)
        return updated

    def revoke(self, request_id: str, *, revoked_by: str) -> ApprovalRequest:
        """Revoke an existing approval."""
        req = self.load_request(request_id)

        if req.status not in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED):
            raise GuardrailError(
                f"Cannot revoke request {request_id}: status is {req.status.value}"
            )

        now = datetime.now(timezone.utc)
        updated = ApprovalRequest(
            id=req.id,
            rule_ids=req.rule_ids,
            diff_hash=req.diff_hash,
            branch=req.branch,
            status=ApprovalStatus.REVOKED,
            requested_by=req.requested_by,
            requested_at=req.requested_at,
            affected_files=req.affected_files,
            approved_by=req.approved_by,
            approved_at=req.approved_at,
            reason=req.reason,
            expires_at=req.expires_at,
            revoked_by=revoked_by,
            revoked_at=now.isoformat(),
        )

        self._write(updated)
        return updated

    def load_request(self, request_id: str) -> ApprovalRequest:
        """Load a single approval request by ID."""
        path = self._dir / f"{request_id}.yaml"
        if not path.exists():
            raise GuardrailError(f"Approval request not found: {request_id}")
        return self._read(path)

    def list_all(self) -> list[ApprovalRequest]:
        """Return all approval requests, marking expired ones."""
        if not self._dir.exists():
            return []

        results: list[ApprovalRequest] = []
        now = datetime.now(timezone.utc)

        for path in sorted(self._dir.glob("apr-*.yaml")):
            try:
                req = self._read(path)
                # Auto-detect expired
                if req.status == ApprovalStatus.APPROVED and req.expires_at:
                    try:
                        if now >= datetime.fromisoformat(req.expires_at):
                            req = ApprovalRequest(
                                id=req.id,
                                rule_ids=req.rule_ids,
                                diff_hash=req.diff_hash,
                                branch=req.branch,
                                status=ApprovalStatus.EXPIRED,
                                requested_by=req.requested_by,
                                requested_at=req.requested_at,
                                affected_files=req.affected_files,
                                approved_by=req.approved_by,
                                approved_at=req.approved_at,
                                reason=req.reason,
                                expires_at=req.expires_at,
                            )
                    except ValueError:
                        pass
                results.append(req)
            except Exception:
                logger.warning("Skipping invalid approval file: %s", path, exc_info=True)

        return results

    def find_matching(
        self, *, rule_ids: set[str], branch: str,
    ) -> list[ApprovalRequest]:
        """Return approved, non-expired requests matching any of *rule_ids* on *branch*."""
        now = datetime.now(timezone.utc)
        matches: list[ApprovalRequest] = []

        for req in self.list_all():
            for rid in rule_ids:
                if req.is_valid_for(rid, branch, now):
                    matches.append(req)
                    break

        return matches

    def clean_expired(self, *, dry_run: bool = False) -> list[str]:
        """Delete expired and revoked approval files.  Returns removed IDs."""
        removed: list[str] = []
        now = datetime.now(timezone.utc)

        if not self._dir.exists():
            return removed

        for path in self._dir.glob("apr-*.yaml"):
            try:
                req = self._read(path)
                should_remove = req.status == ApprovalStatus.REVOKED
                if not should_remove and req.expires_at:
                    try:
                        should_remove = now >= datetime.fromisoformat(req.expires_at)
                    except ValueError:
                        pass
                if should_remove:
                    removed.append(req.id)
                    if not dry_run:
                        path.unlink()
            except Exception:
                logger.warning("Skipping invalid approval file: %s", path, exc_info=True)

        return removed

    # ------------------------------------------------------------------
    # Diff hashing
    # ------------------------------------------------------------------

    @staticmethod
    def compute_diff_hash(diff_text: str) -> str:
        """Deterministic SHA-256 hash of normalized diff content."""
        lines = diff_text.splitlines()
        normalized = "\n".join(line.rstrip() for line in lines).strip()
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    # ------------------------------------------------------------------
    # File I/O helpers
    # ------------------------------------------------------------------

    def _write(self, req: ApprovalRequest) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{req.id}.yaml"
        data = req.to_dict()
        path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")
        return path

    def _read(self, path: Path) -> ApprovalRequest:
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise GuardrailError(f"Invalid approval file: {path}")

        return ApprovalRequest(
            id=raw["id"],
            rule_ids=tuple(raw.get("rule_ids", [])),
            diff_hash=raw.get("diff_hash", ""),
            branch=raw.get("branch", ""),
            status=ApprovalStatus(raw.get("status", "pending")),
            requested_by=raw.get("requested_by", ""),
            requested_at=raw.get("requested_at", ""),
            affected_files=tuple(raw.get("affected_files", [])),
            approved_by=raw.get("approved_by"),
            approved_at=raw.get("approved_at"),
            reason=raw.get("reason"),
            expires_at=raw.get("expires_at"),
            revoked_by=raw.get("revoked_by"),
            revoked_at=raw.get("revoked_at"),
        )
