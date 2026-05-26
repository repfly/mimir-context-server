from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

from mimir.adapters.http.app import _build_app
from mimir.adapters.http.state import HttpServerState
from mimir.domain.config import EmbeddingConfig, MimirConfig, RepoConfig, VectorDbConfig
from mimir.domain.errors import NoActiveIndexError
from mimir.domain.graph import CodeGraph
from mimir.domain.models import Node, NodeKind
from mimir.runtime import IndexerRuntime, QueryRuntime


class _FakeEmbedder:
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]


def _config(tmp_path) -> MimirConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    return MimirConfig(
        repos=[RepoConfig(name="repo", path=repo, language_hint="python")],
        data_dir=tmp_path / ".mimir",
        embeddings=EmbeddingConfig(model="local:test"),
        vector_db=VectorDbConfig(backend="numpy"),
    )


def _graph(node_id: str, *, raw_code: str = "def handle():\n    return 1\n") -> CodeGraph:
    graph = CodeGraph()
    graph.add_node(Node(
        id="repo:",
        repo="repo",
        kind=NodeKind.REPOSITORY,
        name="repo",
    ))
    graph.add_node(Node(
        id="repo:service.py",
        repo="repo",
        kind=NodeKind.FILE,
        name="service.py",
        path="service.py",
        raw_code=raw_code,
    ))
    graph.add_node(Node(
        id=node_id,
        repo="repo",
        kind=NodeKind.FUNCTION,
        name="handle",
        path="service.py",
        raw_code=raw_code,
        embedding=[1.0, 0.0, 0.0],
    ))
    return graph


def test_query_runtime_status_without_active_index_and_require_graph_fails(tmp_path) -> None:
    runtime = QueryRuntime(_config(tmp_path))
    try:
        status = runtime.status()
        assert status["active_version"] is None
        assert status["graph_nodes"] == 0
        with pytest.raises(NoActiveIndexError):
            runtime.require_graph()
    finally:
        runtime.close()


def test_publish_graph_then_query_runtime_loads_and_reloads_active_version(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _config(tmp_path)
    indexer = IndexerRuntime(cfg)
    monkeypatch.setattr(QueryRuntime, "_build_embedder", lambda self: _FakeEmbedder())
    try:
        first = indexer.publish_graph(_graph("repo:service.py::handle"))
        query = QueryRuntime(cfg)
        try:
            assert query.active_version.version == first.version
            assert query.require_graph().has_node("repo:service.py::handle")
            assert query.status()["embedding_model"] == "local:test"

            second = indexer.publish_graph(_graph("repo:service.py::handle_v2", raw_code="def handle_v2():\n    return 2\n"))
            assert second.version != first.version
            assert query.active_version.version == first.version
        finally:
            query.close()
    finally:
        indexer.close()


@pytest.mark.asyncio
async def test_query_runtime_reload_if_changed_swaps_graph(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _config(tmp_path)
    indexer = IndexerRuntime(cfg)
    monkeypatch.setattr(QueryRuntime, "_build_embedder", lambda self: _FakeEmbedder())
    try:
        first = indexer.publish_graph(_graph("repo:service.py::handle"))
        query = QueryRuntime(cfg)
        try:
            second = indexer.publish_graph(_graph("repo:service.py::handle_v2", raw_code="def handle_v2():\n    return 2\n"))
            assert await query.reload_if_changed() is True
            assert query.active_version.version == second.version
            assert query.require_graph().has_node("repo:service.py::handle_v2")
            assert not query.require_graph().has_node("repo:service.py::handle")
        finally:
            query.close()
    finally:
        indexer.close()


@pytest.mark.asyncio
async def test_http_status_without_active_index_and_context_503(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(QueryRuntime, "_build_embedder", lambda self: _FakeEmbedder())
    runtime = QueryRuntime(_config(tmp_path))
    app = _build_app(HttpServerState(runtime=runtime, workspace_name="default"))
    try:
        health = await _request(app, "GET", "/api/v1/health")
        assert health.status == 200
        assert health.json["active_version"] is None

        status = await _request(app, "GET", "/api/v1/index/status")
        assert status.status == 200
        assert status.json["active_version"] is None

        context = await _request(app, "POST", "/api/v1/context", json_body={"query": "handle"})
        assert context.status == 503
        assert context.json["error"] == "No active index"
    finally:
        await app.cleanup()


@pytest.mark.asyncio
async def test_http_index_status_reports_active_version(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _config(tmp_path)
    indexer = IndexerRuntime(cfg)
    monkeypatch.setattr(QueryRuntime, "_build_embedder", lambda self: _FakeEmbedder())
    try:
        version = indexer.publish_graph(_graph("repo:service.py::handle"))
        runtime = QueryRuntime(cfg)
        app = _build_app(HttpServerState(runtime=runtime, workspace_name="default"))
        try:
            response = await _request(app, "GET", "/api/v1/index/status")
            assert response.status == 200
            payload = response.json
            assert payload["active_version"] == version.version
            assert payload["graph_nodes"] == 3
            assert payload["embedding_model"] == "local:test"
        finally:
            await app.cleanup()
    finally:
        indexer.close()


class _Response:
    def __init__(self, response) -> None:
        import json

        self.status = response.status
        self.json = json.loads(response.text)


async def _request(app, method: str, path: str, *, json_body: dict | None = None) -> _Response:
    import json
    from aiohttp import streams

    payload = streams.StreamReader(protocol=SimpleNamespace(_reading_paused=False), limit=2**16)
    if json_body is not None:
        payload.feed_data(json.dumps(json_body).encode("utf-8"))
    payload.feed_eof()
    request = make_mocked_request(
        method,
        path,
        headers={"Content-Type": "application/json"},
        app=app,
        payload=payload,
    )
    match = await app.router.resolve(request)
    request._match_info = match
    response = await match.handler(request)
    return _Response(response)
