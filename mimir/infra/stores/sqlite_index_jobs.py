"""SQLite-backed durable indexer jobs."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional
from uuid import uuid4

from mimir.domain.errors import StorageError
from mimir.domain.index_state import IndexJob, IndexJobKind, IndexJobStatus, utc_now_iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS index_jobs (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    repo       TEXT,
    commit_sha TEXT,
    status     TEXT NOT NULL,
    error      TEXT,
    result     TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_index_jobs_status ON index_jobs(status);
CREATE INDEX IF NOT EXISTS idx_index_jobs_created ON index_jobs(created_at);
"""


class SqliteIndexJobStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(str(db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to initialise index job store: {exc}") from exc

    def enqueue(self, kind: IndexJobKind, *, repo: str | None = None, commit_sha: str | None = None) -> IndexJob:
        existing = self._find_queued(kind, repo)
        if existing is not None:
            if commit_sha:
                existing.commit_sha = commit_sha
                self._update(existing)
            return existing

        now = utc_now_iso()
        job = IndexJob(
            id=f"job-{uuid4().hex[:12]}",
            kind=kind,
            repo=repo,
            commit_sha=commit_sha,
            created_at=now,
            updated_at=now,
        )
        try:
            self._conn.execute(
                "INSERT INTO index_jobs "
                "(id, kind, repo, commit_sha, status, error, result, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job.id,
                    job.kind.value,
                    job.repo,
                    job.commit_sha,
                    job.status.value,
                    job.error,
                    json.dumps(job.result),
                    job.created_at,
                    job.updated_at,
                ),
            )
            self._conn.commit()
            return job
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to enqueue index job: {exc}") from exc

    def claim_next(self) -> Optional[IndexJob]:
        try:
            cur = self._conn.execute(
                "SELECT id, kind, repo, commit_sha, status, error, result, created_at, updated_at "
                "FROM index_jobs WHERE status = ? ORDER BY created_at ASC LIMIT 1",
                (IndexJobStatus.QUEUED.value,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            job = self._row_to_job(row)
            job.status = IndexJobStatus.RUNNING
            job.updated_at = utc_now_iso()
            self._update(job)
            return job
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to claim index job: {exc}") from exc

    def complete(self, job_id: str, result: dict) -> None:
        self._set_terminal(job_id, IndexJobStatus.COMPLETED, result=result, error=None)

    def fail(self, job_id: str, error: str) -> None:
        self._set_terminal(job_id, IndexJobStatus.FAILED, result={}, error=error)

    def get(self, job_id: str) -> Optional[IndexJob]:
        try:
            cur = self._conn.execute(
                "SELECT id, kind, repo, commit_sha, status, error, result, created_at, updated_at "
                "FROM index_jobs WHERE id = ?",
                (job_id,),
            )
            row = cur.fetchone()
            return self._row_to_job(row) if row else None
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to read index job: {exc}") from exc

    def list_jobs(self, status: IndexJobStatus | None = None, limit: int = 100) -> list[IndexJob]:
        try:
            if status is None:
                cur = self._conn.execute(
                    "SELECT id, kind, repo, commit_sha, status, error, result, created_at, updated_at "
                    "FROM index_jobs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            else:
                cur = self._conn.execute(
                    "SELECT id, kind, repo, commit_sha, status, error, result, created_at, updated_at "
                    "FROM index_jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status.value, limit),
                )
            return [self._row_to_job(row) for row in cur.fetchall()]
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to list index jobs: {exc}") from exc

    def _find_queued(self, kind: IndexJobKind, repo: str | None) -> Optional[IndexJob]:
        cur = self._conn.execute(
            "SELECT id, kind, repo, commit_sha, status, error, result, created_at, updated_at "
            "FROM index_jobs WHERE kind = ? AND status = ? AND (repo IS ? OR repo = ?) "
            "ORDER BY created_at DESC LIMIT 1",
            (kind.value, IndexJobStatus.QUEUED.value, repo, repo),
        )
        row = cur.fetchone()
        return self._row_to_job(row) if row else None

    def _set_terminal(
        self,
        job_id: str,
        status: IndexJobStatus,
        *,
        result: dict,
        error: str | None,
    ) -> None:
        try:
            self._conn.execute(
                "UPDATE index_jobs SET status = ?, result = ?, error = ?, updated_at = ? WHERE id = ?",
                (status.value, json.dumps(result), error, utc_now_iso(), job_id),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to update index job: {exc}") from exc

    def _update(self, job: IndexJob) -> None:
        self._conn.execute(
            "UPDATE index_jobs SET kind = ?, repo = ?, commit_sha = ?, status = ?, error = ?, "
            "result = ?, updated_at = ? WHERE id = ?",
            (
                job.kind.value,
                job.repo,
                job.commit_sha,
                job.status.value,
                job.error,
                json.dumps(job.result),
                job.updated_at,
                job.id,
            ),
        )
        self._conn.commit()

    @staticmethod
    def _row_to_job(row) -> IndexJob:
        return IndexJob(
            id=row[0],
            kind=IndexJobKind(row[1]),
            repo=row[2],
            commit_sha=row[3],
            status=IndexJobStatus(row[4]),
            error=row[5],
            result=json.loads(row[6] or "{}"),
            created_at=row[7],
            updated_at=row[8],
        )

    def close(self) -> None:
        self._conn.close()
