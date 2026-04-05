"""Tests for the GuardrailReporter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mimir.domain.guardrails import ChangeSet, GuardrailResult, Severity, Violation
from mimir.services.guardrail_report import (
    GuardrailReporter,
    append_audit_entry,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _passed_result() -> GuardrailResult:
    return GuardrailResult(
        violations=(),
        passed=True,
        summary="All checks passed",
        change_set=ChangeSet(affected_files=("src/a.py",)),
        rules_evaluated=3,
    )


def _failed_result() -> GuardrailResult:
    return GuardrailResult(
        violations=(
            Violation(
                rule_id="no-domain-to-infra",
                rule_description="Domain must not import infra",
                severity=Severity.ERROR,
                message="Banned dependency: domain/model.py -> infra/db.py",
                evidence=("source: domain/model.py", "target: infra/db.py"),
                file_path="domain/model.py",
                suggested_fix="Use a port/interface instead",
            ),
            Violation(
                rule_id="protect-auth",
                rule_description="Auth requires review",
                severity=Severity.BLOCK,
                message="File src/auth/login.py matches protected pattern",
                file_path="src/auth/login.py",
            ),
            Violation(
                rule_id="max-inbound",
                rule_description="Max 20 inbound deps",
                severity=Severity.WARNING,
                message="afferent_coupling for User is 25 (threshold: 20)",
                file_path="src/models.py",
            ),
        ),
        passed=False,
        summary="Violations found. 1 error(s). 1 block(s). 1 warning(s)",
        change_set=ChangeSet(
            affected_files=("domain/model.py", "src/auth/login.py", "src/models.py"),
        ),
        rules_evaluated=5,
    )


# ---------------------------------------------------------------------------
# Text format tests
# ---------------------------------------------------------------------------


class TestFormatText:
    def test_passed(self):
        reporter = GuardrailReporter()
        text = reporter.format_text(_passed_result())
        assert "PASSED" in text
        assert "Rules evaluated: 3" in text

    def test_failed(self):
        reporter = GuardrailReporter()
        text = reporter.format_text(_failed_result())
        assert "FAILED" in text
        assert "no-domain-to-infra" in text
        assert "protect-auth" in text
        assert "ERROR" in text
        assert "BLOCK" in text
        assert "WARNING" in text

    def test_suggested_fix_shown(self):
        reporter = GuardrailReporter()
        text = reporter.format_text(_failed_result())
        assert "Use a port/interface" in text


# ---------------------------------------------------------------------------
# JSON format tests
# ---------------------------------------------------------------------------


class TestFormatJson:
    def test_passed(self):
        reporter = GuardrailReporter()
        data = reporter.format_json(_passed_result())
        assert data["passed"] is True
        assert data["violations"] == []

    def test_failed(self):
        reporter = GuardrailReporter()
        data = reporter.format_json(_failed_result())
        assert data["passed"] is False
        assert len(data["violations"]) == 3
        assert data["violations"][0]["rule_id"] == "no-domain-to-infra"

    def test_json_serializable(self):
        reporter = GuardrailReporter()
        data = reporter.format_json(_failed_result())
        # Should not raise
        json.dumps(data)


# ---------------------------------------------------------------------------
# GitHub PR comment tests
# ---------------------------------------------------------------------------


class TestFormatGithubPrComment:
    def test_passed(self):
        reporter = GuardrailReporter()
        md = reporter.format_github_pr_comment(_passed_result())
        assert "Passed" in md
        assert "No architectural violations" in md

    def test_failed(self):
        reporter = GuardrailReporter()
        md = reporter.format_github_pr_comment(_failed_result())
        assert "Failed" in md
        assert "| Severity" in md  # Table header
        assert "`no-domain-to-infra`" in md
        assert "`protect-auth`" in md
        assert "Suggested Fixes" in md

    def test_is_valid_markdown(self):
        reporter = GuardrailReporter()
        md = reporter.format_github_pr_comment(_failed_result())
        # Should have proper table structure
        lines = md.split("\n")
        table_rows = [l for l in lines if l.startswith("|")]
        assert len(table_rows) >= 4  # header + separator + 3 violations


# ---------------------------------------------------------------------------
# Audit log tests
# ---------------------------------------------------------------------------


class TestFormatAuditLog:
    def test_structure(self):
        reporter = GuardrailReporter()
        entry = reporter.format_audit_log(
            _failed_result(), agent_id="claude-code", change_hash="abc123",
        )
        assert entry["agent_id"] == "claude-code"
        assert entry["change_hash"] == "abc123"
        assert entry["passed"] is False
        assert entry["violation_count"] == 3
        assert "timestamp" in entry
        assert len(entry["violations"]) == 3

    def test_without_optional_fields(self):
        reporter = GuardrailReporter()
        entry = reporter.format_audit_log(_passed_result())
        assert entry["agent_id"] is None
        assert entry["change_hash"] is None
        assert entry["passed"] is True


class TestAppendAuditEntry:
    def test_appends_to_file(self, tmp_path: Path):
        entry1 = {"event": "check", "passed": True}
        entry2 = {"event": "check", "passed": False}

        append_audit_entry(tmp_path, entry1)
        append_audit_entry(tmp_path, entry2)

        audit_file = tmp_path / "guardrail_audit.jsonl"
        assert audit_file.exists()

        lines = audit_file.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["passed"] is True
        assert json.loads(lines[1])["passed"] is False

    def test_creates_directory(self, tmp_path: Path):
        nested = tmp_path / "deep" / "nested"
        append_audit_entry(nested, {"test": True})
        assert (nested / "guardrail_audit.jsonl").exists()
