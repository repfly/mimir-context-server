"""SessionStore port — interface for persisting session state."""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from mimir.domain.session import Session


@runtime_checkable
class SessionStore(Protocol):
    """Interface for durable session storage.

    Implementation: ``SqliteSessionStore``.
    """

    def save(self, session: Session) -> None:
        """Persist a session (upsert by session_id)."""
        ...

    def load(self, session_id: str) -> Optional[Session]:
        """Load a session by ID, or ``None`` if not found."""
        ...

    def list_sessions(self) -> list[str]:
        """Return all stored session IDs."""
        ...

    def delete(self, session_id: str) -> None:
        """Remove a session."""
        ...

    def close(self) -> None:
        """Release resources."""
        ...
