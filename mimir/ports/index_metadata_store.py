"""IndexMetadataStore port for published index versions."""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from mimir.domain.index_state import IndexVersion


@runtime_checkable
class IndexMetadataStore(Protocol):
    def publish(self, version: IndexVersion, *, activate: bool = True) -> None:
        """Record a published index version and optionally make it active."""
        ...

    def get_active(self) -> Optional[IndexVersion]:
        """Return the active published index version, if any."""
        ...

    def get(self, version: str) -> Optional[IndexVersion]:
        """Return a published index version by id."""
        ...

    def list_versions(self, limit: int = 20) -> list[IndexVersion]:
        """Return recently published index versions."""
        ...

    def activate(self, version: str) -> Optional[IndexVersion]:
        """Make an existing published index version active."""
        ...

    def delete(self, version: str) -> None:
        """Delete a published index version metadata row."""
        ...

    def close(self) -> None:
        """Release resources."""
        ...
