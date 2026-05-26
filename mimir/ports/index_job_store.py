"""IndexJobStore port for durable indexer work."""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from mimir.domain.index_state import IndexJob, IndexJobKind, IndexJobStatus


@runtime_checkable
class IndexJobStore(Protocol):
    def enqueue(self, kind: IndexJobKind, *, repo: str | None = None, commit_sha: str | None = None) -> IndexJob:
        """Create or coalesce queued indexer work."""
        ...

    def claim_next(self) -> Optional[IndexJob]:
        """Mark and return the next queued job as running."""
        ...

    def complete(self, job_id: str, result: dict) -> None:
        """Mark a job as completed."""
        ...

    def fail(self, job_id: str, error: str) -> None:
        """Mark a job as failed."""
        ...

    def get(self, job_id: str) -> Optional[IndexJob]:
        """Return a job by id."""
        ...

    def list_jobs(self, status: IndexJobStatus | None = None, limit: int = 100) -> list[IndexJob]:
        """Return recent jobs, optionally filtered by status."""
        ...

    def close(self) -> None:
        """Release resources."""
        ...
