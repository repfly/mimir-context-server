"""Mimir exception hierarchy.

Every infrastructure adapter catches library-specific exceptions and re-raises
as the appropriate Mimir error with context.  The application layer never sees
``chromadb.errors.*`` or ``sqlite3.OperationalError``.
"""

from __future__ import annotations


class TreeDexError(Exception):
    """Base exception for all Mimir errors."""


class ConfigError(TreeDexError):
    """Invalid configuration: missing keys, bad values, unreachable paths."""


class IndexingError(TreeDexError):
    """Failure during the indexing pipeline."""


class ParsingError(IndexingError):
    """A single file could not be parsed (LSP or tree-sitter)."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        super().__init__(f"Failed to parse {path}: {reason}")


class EmbeddingError(IndexingError):
    """Embedding API call failed or returned unexpected results."""


class StorageError(TreeDexError):
    """Database read/write failure (SQLite, ChromaDB, etc.)."""


class RetrievalError(TreeDexError):
    """Failure during context assembly / search."""


class SessionError(TreeDexError):
    """Failure in session management."""
