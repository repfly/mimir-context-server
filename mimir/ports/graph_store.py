"""GraphStore port — interface for persisting the code graph."""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from mimir.domain.graph import CodeGraph


@runtime_checkable
class GraphStore(Protocol):
    """Interface for durable graph storage.

    Implementation: ``SqliteGraphStore``.
    """

    def save(self, graph: CodeGraph) -> None:
        """Persist the entire graph (full overwrite)."""
        ...

    def load(self) -> CodeGraph:
        """Load the full graph from storage.

        Returns an empty ``CodeGraph`` if nothing has been persisted yet.
        """
        ...

    def save_partial(self, nodes: list, edges: list) -> None:
        """Insert or replace specific nodes and edges (no full wipe)."""
        ...

    def save_repo_state(self, repo_name: str, commit_hash: str) -> None:
        """Record the last indexed commit for a repository."""
        ...

    def get_repo_state(self, repo_name: str) -> Optional[str]:
        """Return the last indexed commit hash, or ``None``."""
        ...

    def delete_nodes_by_ids(self, node_ids: list[str]) -> None:
        """Delete specific nodes (and their edges/embeddings) by ID."""
        ...

    def get_all_repo_states(self) -> dict[str, str]:
        """Return ``{repo_name: commit_hash}`` for every tracked repo."""
        ...

    def update_retrieval_metadata(self, nodes: list) -> None:
        """Persist retrieval counters and timestamps for the given nodes."""
        ...

    def vacuum(self) -> None:
        """Compact and optimize the storage backend."""
        ...

    def close(self) -> None:
        """Release resources (file handles, connections)."""
        ...
