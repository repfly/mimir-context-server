"""Shared session-aware bundle post-processing for transport adapters."""

from __future__ import annotations

from mimir.domain.subgraph import SubGraph


def apply_session_context(
    container,
    bundle,
    *,
    query: str,
    session_id: str | None,
    budget: int | None,
) -> None:
    """Apply session deduplication, budget fit, and retrieval tracking."""
    if not session_id:
        return

    session = container.session.get_or_create(session_id)
    subgraph = _bundle_to_subgraph(bundle)
    container.session.session_dedup(
        subgraph,
        session,
        query_embedding=bundle.query_embedding,
    )

    effective_budget = budget or container.retrieval.default_token_budget
    container.retrieval.fit_subgraph_to_budget(
        subgraph,
        effective_budget,
        seed_ids=set(),
    )

    bundle.nodes = list(subgraph.nodes.values())
    bundle.edges = subgraph.edges
    bundle.token_count = subgraph.token_estimate
    if subgraph.notes:
        bundle.session_note = "Previously seen chunks omitted: " + str(len(subgraph.notes))

    container.session.record_retrieval(
        session,
        query,
        bundle.nodes,
        {node.id: 1.0 for node in bundle.nodes},
        query_embedding=bundle.query_embedding,
    )


def _bundle_to_subgraph(bundle) -> SubGraph:
    subgraph = SubGraph()
    for node in bundle.nodes:
        subgraph.add_node(node.clone())
    for edge in bundle.edges:
        subgraph.add_edge(edge)
    return subgraph
