"""Guardrail sub-package — architectural rule evaluation and reporting."""

from mimir.services.guardrail.service import GuardrailService, apply_approvals
from mimir.services.guardrail.diff_analyzer import DiffAnalyzer
from mimir.services.guardrail.report import GuardrailReporter, append_audit_entry
from mimir.services.guardrail.trailers import parse_approval_trailers, read_head_approval

__all__ = [
    "GuardrailService",
    "apply_approvals",
    "DiffAnalyzer",
    "GuardrailReporter",
    "append_audit_entry",
    "parse_approval_trailers",
    "read_head_approval",
]
