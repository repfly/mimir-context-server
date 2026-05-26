from __future__ import annotations

from pathlib import Path

from mimir.domain.index_state import IndexJobKind, IndexJobStatus, IndexVersion
from mimir.infra.stores.sqlite_index_jobs import SqliteIndexJobStore
from mimir.infra.stores.sqlite_index_metadata import SqliteIndexMetadataStore


def test_index_metadata_store_publishes_active_version(tmp_path: Path) -> None:
    store = SqliteIndexMetadataStore(tmp_path / "metadata.db")
    try:
        first = IndexVersion(version="v1", graph_path="/tmp/v1/graph.db", node_count=1)
        second = IndexVersion(
            version="v2",
            graph_path="/tmp/v2/graph.db",
            node_count=2,
            schema_version=3,
            embedding_model="local:test",
            embedding_dim=3,
            config_hash="abc123",
            created_by="test",
        )

        store.publish(first)
        store.publish(second)

        active = store.get_active()
        assert active is not None
        assert active.version == "v2"
        assert active.node_count == 2
        assert active.schema_version == 3
        assert active.embedding_model == "local:test"
        assert active.embedding_dim == 3
        assert active.config_hash == "abc123"
        assert active.created_by == "test"
        assert store.get("v1").active is False
    finally:
        store.close()


def test_index_metadata_store_activates_and_deletes_versions(tmp_path: Path) -> None:
    store = SqliteIndexMetadataStore(tmp_path / "metadata.db")
    try:
        store.publish(IndexVersion(version="v1", graph_path="/tmp/v1/graph.db"), activate=True)
        store.publish(IndexVersion(version="v2", graph_path="/tmp/v2/graph.db"), activate=True)

        activated = store.activate("v1")

        assert activated is not None
        assert activated.active is True
        assert store.get_active().version == "v1"
        assert store.get("v2").active is False

        store.delete("v2")
        assert store.get("v2") is None
    finally:
        store.close()


def test_index_job_store_claims_and_completes_jobs(tmp_path: Path) -> None:
    store = SqliteIndexJobStore(tmp_path / "jobs.db")
    try:
        queued = store.enqueue(IndexJobKind.SYNC, repo="api", commit_sha="abc")

        claimed = store.claim_next()
        assert claimed is not None
        assert claimed.id == queued.id
        assert claimed.kind is IndexJobKind.SYNC
        assert claimed.status is IndexJobStatus.RUNNING

        store.complete(claimed.id, {"version": "v1"})
        completed = store.get(claimed.id)
        assert completed is not None
        assert completed.status is IndexJobStatus.COMPLETED
        assert completed.result == {"version": "v1"}
    finally:
        store.close()


def test_index_job_store_coalesces_queued_jobs_by_kind_and_repo(tmp_path: Path) -> None:
    store = SqliteIndexJobStore(tmp_path / "jobs.db")
    try:
        first = store.enqueue(IndexJobKind.SYNC, repo="api", commit_sha="abc")
        second = store.enqueue(IndexJobKind.SYNC, repo="api", commit_sha="def")

        assert second.id == first.id
        assert second.commit_sha == "def"
        assert len(store.list_jobs()) == 1
    finally:
        store.close()
