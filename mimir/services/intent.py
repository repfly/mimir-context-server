"""Query intent classification — lightweight keyword/pattern classifier.

Routes different query types through different retrieval parameter
profiles so that "where is X?" gets a shallow BM25-heavy search while
"how does X flow into Y?" does deep graph expansion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from mimir.domain.models import EdgeKind


class QueryIntent(str, Enum):
    LOCATE = "locate"
    TRACE = "trace"
    WRITE = "write"
    DEBUG = "debug"
    GENERAL = "general"


@dataclass(frozen=True)
class IntentProfile:
    """Retrieval parameter overrides for a given intent."""

    intent: QueryIntent
    hybrid_alpha: float
    expansion_hops: int
    relevance_gate: float
    priority_edges: list[EdgeKind] = field(default_factory=list)


# Pattern → intent mapping.  Each pattern list is tried in order;
# first match with highest aggregate score wins.
_INTENT_PATTERNS: dict[QueryIntent, list[re.Pattern]] = {
    QueryIntent.LOCATE: [
        re.compile(r"\bwhere\b", re.I),
        re.compile(r"\bfind\b", re.I),
        re.compile(r"\blocate\b", re.I),
        re.compile(r"\bwhich file\b", re.I),
        re.compile(r"\bwhat file\b", re.I),
        re.compile(r"\bdefined\b", re.I),
        re.compile(r"\blives?\b", re.I),
        re.compile(r"\blocation\b", re.I),
    ],
    QueryIntent.TRACE: [
        re.compile(r"\bhow does\b", re.I),
        re.compile(r"\bflow\b", re.I),
        re.compile(r"\btrace\b", re.I),
        re.compile(r"\bcall.?chain\b", re.I),
        re.compile(r"\bwhat happens when\b", re.I),
        re.compile(r"\bstep.?by.?step\b", re.I),
        re.compile(r"\bsequence\b", re.I),
        re.compile(r"\bpipeline\b", re.I),
    ],
    QueryIntent.WRITE: [
        re.compile(r"\badd\b", re.I),
        re.compile(r"\bcreate\b", re.I),
        re.compile(r"\bimplement\b", re.I),
        re.compile(r"\bwrite\b", re.I),
        re.compile(r"\bbuild\b", re.I),
        re.compile(r"\bnew\b", re.I),
        re.compile(r"\bextend\b", re.I),
        re.compile(r"\brefactor\b", re.I),
    ],
    QueryIntent.DEBUG: [
        re.compile(r"\bwhy\b", re.I),
        re.compile(r"\bcrash", re.I),
        re.compile(r"\berror\b", re.I),
        re.compile(r"\bbug\b", re.I),
        re.compile(r"\bfail", re.I),
        re.compile(r"\bfix\b", re.I),
        re.compile(r"\bbroken\b", re.I),
        re.compile(r"\bwrong\b", re.I),
        re.compile(r"\bdebug\b", re.I),
    ],
}

INTENT_PROFILES: dict[QueryIntent, IntentProfile] = {
    QueryIntent.LOCATE: IntentProfile(
        intent=QueryIntent.LOCATE,
        hybrid_alpha=0.4,
        expansion_hops=1,
        relevance_gate=0.35,
        priority_edges=[EdgeKind.CONTAINS, EdgeKind.IMPORTS],
    ),
    QueryIntent.TRACE: IntentProfile(
        intent=QueryIntent.TRACE,
        hybrid_alpha=0.7,
        expansion_hops=3,
        relevance_gate=0.25,
        priority_edges=[EdgeKind.CALLS, EdgeKind.API_CALLS, EdgeKind.USES_TYPE],
    ),
    QueryIntent.WRITE: IntentProfile(
        intent=QueryIntent.WRITE,
        hybrid_alpha=0.6,
        expansion_hops=2,
        relevance_gate=0.3,
        priority_edges=[EdgeKind.INHERITS, EdgeKind.IMPLEMENTS, EdgeKind.USES_TYPE],
    ),
    QueryIntent.DEBUG: IntentProfile(
        intent=QueryIntent.DEBUG,
        hybrid_alpha=0.5,
        expansion_hops=2,
        relevance_gate=0.3,
        priority_edges=[EdgeKind.CALLS, EdgeKind.USES_TYPE, EdgeKind.READS_CONFIG],
    ),
    QueryIntent.GENERAL: IntentProfile(
        intent=QueryIntent.GENERAL,
        hybrid_alpha=0.7,
        expansion_hops=2,
        relevance_gate=0.3,
    ),
}


def classify_intent(query: str) -> QueryIntent:
    """Classify a query into an intent using regex pattern matching.

    Scores each intent by counting how many of its patterns match,
    and returns the highest-scoring intent.  Falls back to GENERAL.
    """
    best_intent = QueryIntent.GENERAL
    best_score = 0

    for intent, patterns in _INTENT_PATTERNS.items():
        score = sum(1 for p in patterns if p.search(query))
        if score > best_score:
            best_score = score
            best_intent = intent

    return best_intent
