"""Domain records for published index versions and indexer jobs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, unique
from typing import Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@unique
class IndexJobKind(str, Enum):
    FULL = "full"
    INCREMENTAL = "incremental"
    SYNC = "sync"


@unique
class IndexJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class IndexVersion:
    version: str
    graph_path: str
    active: bool = False
    node_count: int = 0
    edge_count: int = 0
    repo_commits: dict[str, str] = field(default_factory=dict)
    schema_version: int = 1
    embedding_model: Optional[str] = None
    config_hash: Optional[str] = None
    created_by: Optional[str] = None
    created_at: str = field(default_factory=utc_now_iso)
    activated_at: Optional[str] = None


@dataclass
class IndexJob:
    id: str
    kind: IndexJobKind
    status: IndexJobStatus = IndexJobStatus.QUEUED
    repo: Optional[str] = None
    commit_sha: Optional[str] = None
    error: Optional[str] = None
    result: dict = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
