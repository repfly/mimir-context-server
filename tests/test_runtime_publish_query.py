from __future__ import annotations

from unittest.mock import Mock

import pytest
from aiohttp.test_utils import make_mocked_request

from mimir.adapters.http.app import _build_app
from mimir.adapters.http.state import HttpServerState
from mimir.adapters.web.routes import register_routes as register_web_routes
from mimir.adapters.web.state import WebServerState
from mimir.domain.config import EmbeddingConfig, MimirConfig, RepoConfig
from mimir.domain.errors import NoActiveIndexError
from mimir.domain.graph import CodeGraph
from mimir.domain.models import Node, NodeKind
import mimir.runtime as runtime_module
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
    monkeypatch.setattr(runtime_module, "_build_embedder", lambda config: _FakeEmbedder())
    indexer = IndexerRuntime(cfg)
    try:
        first = indexer.publish_graph(_graph("repo:service.py::handle"))
        query = QueryRuntime(cfg)
        try:
            assert query.active_version.version == first.version
            assert query.require_graph().has_node("repo:service.py::handle")
            assert query.status()["embedding_model"] == "local:test"
            assert query.status()["embedding_dim"] == 3

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
    monkeypatch.setattr(runtime_module, "_build_embedder", lambda config: _FakeEmbedder())
    indexer = IndexerRuntime(cfg)
    try:
        indexer.publish_graph(_graph("repo:service.py::handle"))
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
    monkeypatch.setattr(runtime_module, "_build_embedder", lambda config: _FakeEmbedder())
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
    monkeypatch.setattr(runtime_module, "_build_embedder", lambda config: _FakeEmbedder())
    indexer = IndexerRuntime(cfg)
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


def test_indexer_lifecycle_can_activate_and_prune_versions(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _config(tmp_path)
    monkeypatch.setattr(runtime_module, "_build_embedder", lambda config: _FakeEmbedder())
    indexer = IndexerRuntime(cfg)
    try:
        first = indexer.publish_graph(_graph("repo:service.py::handle"))
        second = indexer.publish_graph(_graph("repo:service.py::handle_v2"))
        third = indexer.publish_graph(_graph("repo:service.py::handle_v3"))

        activated = indexer.activate_version(first.version)
        removed = indexer.prune_versions(keep=1)

        assert activated.version == first.version
        assert first.version not in removed
        assert second.version in removed
        assert third.version not in removed
    finally:
        indexer.close()


def test_query_runtime_rejects_embedding_model_mismatch(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _config(tmp_path)
    monkeypatch.setattr(runtime_module, "_build_embedder", lambda config: _FakeEmbedder())
    indexer = IndexerRuntime(cfg)
    try:
        indexer.publish_graph(_graph("repo:service.py::handle"))
    finally:
        indexer.close()

    mismatched = MimirConfig(
        repos=cfg.repos,
        data_dir=cfg.data_dir,
        embeddings=EmbeddingConfig(model="local:other"),
    )
    with pytest.raises(Exception, match="embedding model mismatch"):
        QueryRuntime(mismatched)


@pytest.mark.asyncio
async def test_mcp_over_http_without_active_index_returns_503(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_module, "_build_embedder", lambda config: _FakeEmbedder())
    runtime = QueryRuntime(_config(tmp_path))
    app = _build_app(HttpServerState(runtime=runtime, workspace_name="default"))
    try:
        response = await _request(app, "POST", "/api/v1/mcp", json_body={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_graph_stats", "arguments": {}},
        })

        assert response.status == 503
        assert response.json["error"]["message"].startswith("No active index")
    finally:
        await app.cleanup()


@pytest.mark.asyncio
async def test_web_inspector_without_active_index_returns_503(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_module, "_build_embedder", lambda config: _FakeEmbedder())
    runtime = QueryRuntime(_config(tmp_path))
    app = _build_web_app(runtime)
    try:
        response = await _request(app, "GET", "/api/stats")

        assert response.status == 503
        assert response.json["error"] == "No active index"
    finally:
        await app.cleanup()


class _Response:
    def __init__(self, response) -> None:
        import json

        self.status = response.status
        self.json = json.loads(response.text)


async def _request(app, method: str, path: str, *, json_body: dict | None = None) -> _Response:
    import json
    from aiohttp import streams

    # Mock protocol: aiohttp>=3.14 calls flow-control hooks (e.g. resume_reading) on it.
    payload = streams.StreamReader(protocol=Mock(_reading_paused=False), limit=2**16)
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


def _build_web_app(runtime: QueryRuntime):
    from aiohttp import web

    routes = web.RouteTableDef()
    register_web_routes(routes, WebServerState(runtime=runtime))
    app = web.Application()
    app.router.add_routes(routes)
    return app
