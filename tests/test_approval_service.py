"""Tests for the ApprovalService."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mimir.domain.approvals import ApprovalStatus
from mimir.domain.errors import GuardrailError
from mimir.services.approval import ApprovalService


class TestComputeDiffHash:
    def test_deterministic(self):
        h1 = ApprovalService.compute_diff_hash("diff --git a/foo\n+bar")
        h2 = ApprovalService.compute_diff_hash("diff --git a/foo\n+bar")
        assert h1 == h2
        assert h1.startswith("sha256:")

    def test_trailing_whitespace_normalized(self):
        h1 = ApprovalService.compute_diff_hash("line1  \nline2\t\n")
        h2 = ApprovalService.compute_diff_hash("line1\nline2\n")
        assert h1 == h2

    def test_different_content_different_hash(self):
        h1 = ApprovalService.compute_diff_hash("aaa")
        h2 = ApprovalService.compute_diff_hash("bbb")
        assert h1 != h2


class TestCreateRequest:
    def test_creates_file(self, tmp_path: Path):
        svc = ApprovalService(tmp_path)
        req = svc.create_request(
            rule_ids=["protect-container"],
            diff_text="diff content",
            requested_by="alice",
            affected_files=["container.py"],
        )
        assert req.id.startswith("apr-")
        assert req.status == ApprovalStatus.PENDING
        assert req.requested_by == "alice"
        assert req.rule_ids == ("protect-container",)
        assert (tmp_path / f"{req.id}.yaml").exists()

    def test_creates_dir_if_missing(self, tmp_path: Path):
        svc = ApprovalService(tmp_path / "nested" / "approvals")
        req = svc.create_request(
            rule_ids=["r1"], diff_text="diff", requested_by="bob",
        )
        assert (tmp_path / "nested" / "approvals" / f"{req.id}.yaml").exists()

    def test_custom_ttl(self, tmp_path: Path):
        svc = ApprovalService(tmp_path)
        req = svc.create_request(
            rule_ids=["r1"], diff_text="diff", requested_by="alice", ttl_days=14,
        )
        assert req.expires_at is not None
        expiry = datetime.fromisoformat(req.expires_at)
        requested = datetime.fromisoformat(req.requested_at)
        assert (expiry - requested).days == 14


class TestApprove:
    def test_approve_pending(self, tmp_path: Path):
        svc = ApprovalService(tmp_path)
        req = svc.create_request(
            rule_ids=["r1"], diff_text="diff", requested_by="alice",
        )
        approved = svc.approve(req.id, approved_by="bob", reason="Looks good")
        assert approved.status == ApprovalStatus.APPROVED
        assert approved.approved_by == "bob"
        assert approved.reason == "Looks good"

    def test_approve_already_approved_raises(self, tmp_path: Path):
        svc = ApprovalService(tmp_path)
        req = svc.create_request(
            rule_ids=["r1"], diff_text="diff", requested_by="alice",
        )
        svc.approve(req.id, approved_by="bob", reason="ok")
        with pytest.raises(GuardrailError, match="Cannot approve"):
            svc.approve(req.id, approved_by="bob", reason="again")

    def test_approve_unauthorized_raises(self, tmp_path: Path):
        svc = ApprovalService(tmp_path)
        req = svc.create_request(
            rule_ids=["r1"], diff_text="diff", requested_by="alice",
        )
        with pytest.raises(GuardrailError, match="not in the authorized"):
            svc.approve(
                req.id, approved_by="eve", reason="ok",
                approvers_allowed=["bob"],
            )

    def test_approve_with_allowed_list(self, tmp_path: Path):
        svc = ApprovalService(tmp_path)
        req = svc.create_request(
            rule_ids=["r1"], diff_text="diff", requested_by="alice",
        )
        approved = svc.approve(
            req.id, approved_by="bob", reason="ok",
            approvers_allowed=["bob", "charlie"],
        )
        assert approved.status == ApprovalStatus.APPROVED


class TestRevoke:
    def test_revoke_approved(self, tmp_path: Path):
        svc = ApprovalService(tmp_path)
        req = svc.create_request(
            rule_ids=["r1"], diff_text="diff", requested_by="alice",
        )
        svc.approve(req.id, approved_by="bob", reason="ok")
        revoked = svc.revoke(req.id, revoked_by="charlie")
        assert revoked.status == ApprovalStatus.REVOKED
        assert revoked.revoked_by == "charlie"

    def test_revoke_already_revoked_raises(self, tmp_path: Path):
        svc = ApprovalService(tmp_path)
        req = svc.create_request(
            rule_ids=["r1"], diff_text="diff", requested_by="alice",
        )
        svc.revoke(req.id, revoked_by="bob")
        with pytest.raises(GuardrailError, match="Cannot revoke"):
            svc.revoke(req.id, revoked_by="bob")


class TestLoadAndList:
    def test_load_nonexistent_raises(self, tmp_path: Path):
        svc = ApprovalService(tmp_path)
        with pytest.raises(GuardrailError, match="not found"):
            svc.load_request("apr-nonexistent")

    def test_list_empty(self, tmp_path: Path):
        svc = ApprovalService(tmp_path)
        assert svc.list_all() == []

    def test_list_all(self, tmp_path: Path):
        svc = ApprovalService(tmp_path)
        svc.create_request(rule_ids=["r1"], diff_text="d1", requested_by="a")
        svc.create_request(rule_ids=["r2"], diff_text="d2", requested_by="b")
        assert len(svc.list_all()) == 2


class TestFindMatching:
    def test_finds_matching_approval(self, tmp_path: Path):
        svc = ApprovalService(tmp_path)
        req = svc.create_request(
            rule_ids=["r1"], diff_text="the diff", requested_by="alice",
        )
        svc.approve(req.id, approved_by="bob", reason="ok")

        diff_hash = ApprovalService.compute_diff_hash("the diff")
        matches = svc.find_matching(rule_ids={"r1"}, diff_hash=diff_hash)
        assert len(matches) == 1
        assert matches[0].id == req.id

    def test_no_match_wrong_hash(self, tmp_path: Path):
        svc = ApprovalService(tmp_path)
        req = svc.create_request(
            rule_ids=["r1"], diff_text="the diff", requested_by="alice",
        )
        svc.approve(req.id, approved_by="bob", reason="ok")

        matches = svc.find_matching(rule_ids={"r1"}, diff_hash="sha256:wrong")
        assert len(matches) == 0

    def test_no_match_pending(self, tmp_path: Path):
        svc = ApprovalService(tmp_path)
        svc.create_request(
            rule_ids=["r1"], diff_text="the diff", requested_by="alice",
        )
        diff_hash = ApprovalService.compute_diff_hash("the diff")
        matches = svc.find_matching(rule_ids={"r1"}, diff_hash=diff_hash)
        assert len(matches) == 0  # pending, not approved


class TestCleanExpired:
    def test_clean_revoked(self, tmp_path: Path):
        svc = ApprovalService(tmp_path)
        req = svc.create_request(
            rule_ids=["r1"], diff_text="diff", requested_by="alice",
        )
        svc.revoke(req.id, revoked_by="bob")

        removed = svc.clean_expired()
        assert req.id in removed
        assert not (tmp_path / f"{req.id}.yaml").exists()

    def test_clean_dry_run(self, tmp_path: Path):
        svc = ApprovalService(tmp_path)
        req = svc.create_request(
            rule_ids=["r1"], diff_text="diff", requested_by="alice",
        )
        svc.revoke(req.id, revoked_by="bob")

        removed = svc.clean_expired(dry_run=True)
        assert req.id in removed
        assert (tmp_path / f"{req.id}.yaml").exists()  # still exists

    def test_clean_empty(self, tmp_path: Path):
        svc = ApprovalService(tmp_path)
        assert svc.clean_expired() == []
