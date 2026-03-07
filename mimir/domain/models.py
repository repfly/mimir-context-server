"""Core domain models for the unified code graph.

All dataclasses are frozen where possible to enforce immutability in the domain
layer.  Mutable runtime state (retrieval counts, co-retrieval maps) is tracked
outside the domain on dedicated service-level structures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum, unique
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Node kinds
# ---------------------------------------------------------------------------

@unique
class NodeKind(Enum):
    """Classification of nodes in the code graph."""

    REPOSITORY = "repository"
    MODULE = "module"
    FILE = "file"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    TYPE = "type"
    CONSTANT = "constant"
    API_ENDPOINT = "api_endpoint"
    CONFIG = "config"


#: Kinds that represent actual code symbols (leaf-level embedding targets).
SYMBOL_KINDS: frozenset[NodeKind] = frozenset({
    NodeKind.FUNCTION,
    NodeKind.METHOD,
    NodeKind.CLASS,
    NodeKind.TYPE,
    NodeKind.CONSTANT,
})

#: Kinds that represent structural containers (hierarchy embedding targets).
CONTAINER_KINDS: frozenset[NodeKind] = frozenset({
    NodeKind.REPOSITORY,
    NodeKind.MODULE,
    NodeKind.FILE,
})


# ---------------------------------------------------------------------------
# Edge kinds
# ---------------------------------------------------------------------------

@unique
class EdgeKind(Enum):
    """Classification of edges in the code graph."""

    # Containment
    CONTAINS = "contains"

    # Intra-repo dependencies
    CALLS = "calls"
    IMPORTS = "imports"
    INHERITS = "inherits"
    IMPLEMENTS = "implements"
    USES_TYPE = "uses_type"
    READS_CONFIG = "reads_config"
    EXPOSES_API = "exposes_api"

    # Cross-repo dependencies
    API_CALLS = "api_calls"
    SHARED_LIB = "shared_lib"
    PROTO_DEFINES = "proto_defines"


#: Edge kinds that cross repository boundaries.
CROSS_REPO_EDGE_KINDS: frozenset[EdgeKind] = frozenset({
    EdgeKind.API_CALLS,
    EdgeKind.SHARED_LIB,
    EdgeKind.PROTO_DEFINES,
})

#: Default weights for subgraph expansion, ordered by traversal priority.
EDGE_EXPANSION_WEIGHTS: dict[EdgeKind, float] = {
    EdgeKind.CALLS: 1.0,
    EdgeKind.API_CALLS: 1.0,
    EdgeKind.USES_TYPE: 0.8,
    EdgeKind.READS_CONFIG: 0.6,
    EdgeKind.IMPORTS: 0.4,
    EdgeKind.INHERITS: 0.9,
    EdgeKind.IMPLEMENTS: 0.9,
    EdgeKind.CONTAINS: 0.3,
    EdgeKind.EXPOSES_API: 0.7,
    EdgeKind.SHARED_LIB: 0.5,
    EdgeKind.PROTO_DEFINES: 0.5,
}


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

@dataclass
class Node:
    """A single element in the unified code graph.

    Parameters
    ----------
    id
        Globally unique identifier in the form
        ``"repo_name:relative/path.py::symbol_name"``.
    repo
        Repository this node belongs to.
    kind
        The semantic kind of this node.
    name
        Human-readable short name (e.g. function name).
    """

    id: str
    repo: str
    kind: NodeKind
    name: str

    # Location
    path: Optional[str] = None
    start_line: Optional[int] = None
    end_line: Optional[int] = None

    # Content
    summary: Optional[str] = None
    raw_code: Optional[str] = None
    signature: Optional[str] = None
    docstring: Optional[str] = None

    # Embedding (stored separately in vector store, cached here for speed)
    embedding: Optional[list[float]] = None

    # Git metadata
    last_modified: Optional[str] = None
    modification_count: int = 0

    # Runtime retrieval metadata (mutable, updated by TemporalService)
    last_retrieved: Optional[str] = None
    retrieval_count: int = 0
    co_retrieved_with: dict[str, int] = field(default_factory=dict)

    # ---- helpers -----------------------------------------------------------

    @property
    def token_estimate(self) -> int:
        """Rough token count: ~4 chars per token for code."""
        text = self.raw_code or self.summary or ""
        return max(1, len(text) // 4)

    @property
    def has_code(self) -> bool:
        return self.raw_code is not None and len(self.raw_code) > 0

    @property
    def is_symbol(self) -> bool:
        return self.kind in SYMBOL_KINDS

    @property
    def is_container(self) -> bool:
        return self.kind in CONTAINER_KINDS

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dictionary (excludes embedding)."""
        return {
            "id": self.id,
            "repo": self.repo,
            "kind": self.kind.value,
            "name": self.name,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "summary": self.summary,
            "raw_code": self.raw_code,
            "signature": self.signature,
            "docstring": self.docstring,
            "last_modified": self.last_modified,
            "modification_count": self.modification_count,
            "last_retrieved": self.last_retrieved,
            "retrieval_count": self.retrieval_count,
            "co_retrieved_with": self.co_retrieved_with,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Node:
        """Deserialise from a dictionary."""
        data = dict(data)  # shallow copy to avoid mutation
        data["kind"] = NodeKind(data["kind"])
        # Drop unknown keys gracefully
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def __repr__(self) -> str:
        return f"Node({self.kind.value}:{self.id})"


# ---------------------------------------------------------------------------
# Edge
# ---------------------------------------------------------------------------

@dataclass
class Edge:
    """A directed relationship in the code graph."""

    source: str
    target: str
    kind: EdgeKind
    weight: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "kind": self.kind.value,
            "weight": self.weight,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Edge:
        data = dict(data)
        data["kind"] = EdgeKind(data["kind"])
        if isinstance(data.get("metadata"), str):
            data["metadata"] = json.loads(data["metadata"])
        return cls(**data)

    @property
    def is_cross_repo(self) -> bool:
        return self.kind in CROSS_REPO_EDGE_KINDS

    def __repr__(self) -> str:
        return f"Edge({self.source} --{self.kind.value}--> {self.target})"
