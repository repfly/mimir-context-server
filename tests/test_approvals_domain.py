"""Tests for approval domain models."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mimir.domain.approvals import ApprovalConfig, ApprovalRequest, ApprovalStatus


class TestApprovalStatus:
    def test_all_values(self):
        assert len(ApprovalStatus) == 4
        assert ApprovalStatus("pending") == ApprovalStatus.PENDING
        assert ApprovalStatus("approved") == ApprovalStatus.APPROVED
        assert ApprovalStatus("revoked") == ApprovalStatus.REVOKED
        assert ApprovalStatus("expired") == ApprovalStatus.EXPIRED


class TestApprovalRequest:
    def _make(self, **overrides) -> ApprovalRequest:
        defaults = dict(
            id="apr-12345678",
            rule_ids=("protect-container",),
            diff_hash="sha256:abc123",
            branch="feature/guardrail",
            status=ApprovalStatus.PENDING,
            requested_by="alice",
            requested_at="2026-04-06T10:00:00+00:00",
        )
        defaults.update(overrides)
        return ApprovalRequest(**defaults)

    def test_create(self):
        req = self._make()
        assert req.id == "apr-12345678"
        assert req.rule_ids == ("protect-container",)
        assert req.status == ApprovalStatus.PENDING

    def test_frozen(self):
        req = self._make()
        with pytest.raises(AttributeError):
            req.status = ApprovalStatus.APPROVED  # type: ignore[misc]

    def test_empty_id_raises(self):
        with pytest.raises(ValueError, match="id must not be empty"):
            self._make(id="")

    def test_empty_rule_ids_raises(self):
        with pytest.raises(ValueError, match="at least one rule"):
            self._make(rule_ids=())

    def test_to_dict(self):
        req = self._make(affected_files=("a.py",))
        d = req.to_dict()
        assert d["id"] == "apr-12345678"
        assert d["rule_ids"] == ["protect-container"]
        assert d["branch"] == "feature/guardrail"
        assert d["affected_files"] == ["a.py"]
        assert d["status"] == "pending"

    def test_is_valid_for_approved_matching(self):
        now = datetime(2026, 4, 6, 12, 0, tzinfo=timezone.utc)
        req = self._make(
            status=ApprovalStatus.APPROVED,
            branch="feature/guardrail",
            expires_at="2026-04-13T10:00:00+00:00",
        )
        assert req.is_valid_for("protect-container", "feature/guardrail", now) is True

    def test_is_valid_for_wrong_rule(self):
        req = self._make(status=ApprovalStatus.APPROVED, branch="feature/guardrail")
        assert req.is_valid_for("other-rule", "feature/guardrail") is False

    def test_is_valid_for_wrong_branch(self):
        req = self._make(status=ApprovalStatus.APPROVED, branch="feature/guardrail")
        assert req.is_valid_for("protect-container", "other-branch") is False

    def test_is_valid_for_pending_status(self):
        req = self._make(status=ApprovalStatus.PENDING, branch="feature/guardrail")
        assert req.is_valid_for("protect-container", "feature/guardrail") is False

    def test_is_valid_for_expired(self):
        now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
        req = self._make(
            status=ApprovalStatus.APPROVED,
            branch="feature/guardrail",
            expires_at="2026-04-13T10:00:00+00:00",
        )
        assert req.is_valid_for("protect-container", "feature/guardrail", now) is False

    def test_is_valid_for_no_expiry(self):
        req = self._make(
            status=ApprovalStatus.APPROVED,
            branch="feature/guardrail",
            expires_at=None,
        )
        assert req.is_valid_for("protect-container", "feature/guardrail") is True


class TestApprovalConfig:
    def test_defaults(self):
        cfg = ApprovalConfig()
        assert cfg.default_ttl_days == 7
        assert cfg.approvers == ()
        assert cfg.approvals_dir == ".mimir/approvals"

    def test_custom(self):
        cfg = ApprovalConfig(
            default_ttl_days=14,
            approvers=("alice", "bob"),
            approvals_dir="/custom/path",
        )
        assert cfg.default_ttl_days == 14
        assert cfg.approvers == ("alice", "bob")
