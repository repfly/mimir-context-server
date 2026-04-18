"""Parse approval trailers from git commit messages.

An approval for a BLOCK-severity guardrail rule lives entirely in the
HEAD commit message of the branch being checked. There is no persisted
approval object, no registry, no TTL: push a new commit, the approval
is gone.

Trailer format (case-insensitive keys, RFC 2822-style):

    Mimir-Approved: protect-container, protect-ci
    Mimir-Approved-Reason: legal signoff ticket #4821

``Mimir-Approved:`` may repeat and/or contain a comma-separated list of
rule ids. ``Mimir-Approved-Reason:`` must be present and non-empty for
the approval to be considered valid.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


_APPROVED_KEY = "mimir-approved:"
_REASON_KEY = "mimir-approved-reason:"


@dataclass(frozen=True)
class HeadApproval:
    """Approval trailers extracted from a single commit message."""

    rule_ids: frozenset[str]
    reason: str


def parse_approval_trailers(message: str) -> tuple[frozenset[str], str]:
    """Extract approved rule ids and reason from a commit message.

    Returns ``(rule_ids, reason)``. ``rule_ids`` is empty if no
    ``Mimir-Approved:`` trailer is present. ``reason`` is the empty
    string if no ``Mimir-Approved-Reason:`` trailer is present.
    """
    rule_ids: set[str] = set()
    reason = ""

    for raw in message.splitlines():
        line = raw.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith(_APPROVED_KEY) and not lower.startswith(_REASON_KEY):
            value = line.split(":", 1)[1]
            for rid in value.split(","):
                rid_clean = rid.strip()
                if rid_clean:
                    rule_ids.add(rid_clean)
        elif lower.startswith(_REASON_KEY):
            value = line.split(":", 1)[1].strip()
            if value:
                reason = value

    return frozenset(rule_ids), reason


def read_head_approval(head: str = "HEAD") -> HeadApproval | None:
    """Read approval trailers from the HEAD commit via git.

    Returns ``None`` if git is unavailable or the commit has no
    ``Mimir-Approved:`` trailer.
    """
    try:
        message = subprocess.check_output(
            ["git", "log", "-1", "--format=%B", head],
            text=True, stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None

    rule_ids, reason = parse_approval_trailers(message)
    if not rule_ids:
        return None

    return HeadApproval(rule_ids=rule_ids, reason=reason)
