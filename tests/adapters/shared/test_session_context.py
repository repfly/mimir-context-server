from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from mimir.adapters.shared.session_context import apply_session_context
from mimir.domain.models import Node, NodeKind
from mimir.domain.session import ContextEntry, QueryRecord, Session
from mimir.domain.subgraph import ContextBundle
from mimir.services.session import SessionService


class _MemorySessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def save(self, session: Session) -> None:
        self._sessions[session.session_id] = session

    def load(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def list_sessions(self) -> list[str]:
        return sorted(self._sessions)

    def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def close(self) -> None:
        return None


class _FakeRetrieval:
    default_token_budget = 8000

    def fit_subgraph_to_budget(self, subgraph, budget: int, *, seed_ids=None) -> None:
        return None


def test_apply_session_context_does_not_mutate_canonical_node() -> None:
    canonical = Node(
        id="repo:service.py::handle",
        repo="repo",
        kind=NodeKind.FUNCTION,
        name="handle",
        path="service.py",
        raw_code="def handle():\n    return 1\n",
        summary="Handle the request.",
    )
    bundle = ContextBundle(
        nodes=[canonical],
        edges=[],
        summary="context",
        token_count=canonical.token_estimate,
        repos_involved=["repo"],
        query_embedding=None,
    )

    store = _MemorySessionStore()
    session = Session(session_id="s-1")
    session.query_history.append(QueryRecord(
        query="older",
        turn_number=3,
        retrieved_node_ids=[canonical.id],
        timestamp=datetime.now(timezone.utc),
        query_embedding=None,
    ))
    session.context_window[canonical.id] = ContextEntry(
        node_id=canonical.id,
        added_at=datetime.now(timezone.utc),
        turn_number=1,
        relevance_at_addition=1.0,
        query_embedding_at_addition=None,
    )
    store.save(session)

    container = SimpleNamespace(
        session=SessionService(
            config=SimpleNamespace(session=SimpleNamespace(context_decay_turns=5, topic_tracking_alpha=0.3)),
            session_store=store,
        ),
        retrieval=_FakeRetrieval(),
    )

    apply_session_context(
        container,
        bundle,
        query="current",
        session_id="s-1",
        budget=None,
    )

    assert canonical.raw_code == "def handle():\n    return 1\n"
    assert len(bundle.nodes) == 1
    assert bundle.nodes[0].id == canonical.id
    assert bundle.nodes[0].raw_code is None
