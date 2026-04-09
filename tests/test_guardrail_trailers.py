"""Tests for the HEAD-commit approval trailer parser."""

from __future__ import annotations

from mimir.services.guardrail_trailers import parse_approval_trailers


class TestParseApprovalTrailers:
    def test_no_trailers(self):
        rule_ids, reason = parse_approval_trailers("just a normal commit")
        assert rule_ids == frozenset()
        assert reason == ""

    def test_single_rule_single_line(self):
        msg = (
            "approval: protect-container\n"
            "\n"
            "Mimir-Approved: protect-container\n"
            "Mimir-Approved-Reason: legal signoff\n"
        )
        rule_ids, reason = parse_approval_trailers(msg)
        assert rule_ids == frozenset({"protect-container"})
        assert reason == "legal signoff"

    def test_multi_rule_comma_separated(self):
        msg = (
            "approval: multiple rules\n"
            "\n"
            "Mimir-Approved: protect-container, protect-ci, protect-docker\n"
            "Mimir-Approved-Reason: coordinated release\n"
        )
        rule_ids, reason = parse_approval_trailers(msg)
        assert rule_ids == frozenset({"protect-container", "protect-ci", "protect-docker"})
        assert reason == "coordinated release"

    def test_multiple_approved_lines_merge(self):
        msg = (
            "subject\n"
            "\n"
            "Mimir-Approved: r1\n"
            "Mimir-Approved: r2\n"
            "Mimir-Approved-Reason: x\n"
        )
        rule_ids, reason = parse_approval_trailers(msg)
        assert rule_ids == frozenset({"r1", "r2"})
        assert reason == "x"

    def test_case_insensitive_keys(self):
        msg = (
            "MIMIR-APPROVED: r1\n"
            "mimir-approved-reason: because\n"
        )
        rule_ids, reason = parse_approval_trailers(msg)
        assert rule_ids == frozenset({"r1"})
        assert reason == "because"

    def test_empty_reason_not_captured(self):
        msg = (
            "Mimir-Approved: r1\n"
            "Mimir-Approved-Reason:   \n"
        )
        rule_ids, reason = parse_approval_trailers(msg)
        assert rule_ids == frozenset({"r1"})
        assert reason == ""

    def test_whitespace_and_empty_entries_stripped(self):
        msg = "Mimir-Approved: r1, , r2 ,\n"
        rule_ids, reason = parse_approval_trailers(msg)
        assert rule_ids == frozenset({"r1", "r2"})
        assert reason == ""

    def test_reason_only_is_ignored_without_rule(self):
        msg = "Mimir-Approved-Reason: something\n"
        rule_ids, reason = parse_approval_trailers(msg)
        assert rule_ids == frozenset()
        assert reason == "something"

    def test_approved_line_without_reason_key_is_not_parsed_as_reason(self):
        # guard against the reason check eating the approved line
        msg = "Mimir-Approved: r1\n"
        rule_ids, reason = parse_approval_trailers(msg)
        assert rule_ids == frozenset({"r1"})
        assert reason == ""
