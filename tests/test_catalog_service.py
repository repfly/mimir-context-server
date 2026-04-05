"""Tests for the CatalogService."""

import json
from unittest.mock import MagicMock, patch

import pytest

from mimir.domain.graph import CodeGraph
from mimir.domain.models import Edge, EdgeKind, Node, NodeKind
from mimir.services.catalog import CatalogService
from mimir.services.quality import QualityService


def _build_test_graph() -> CodeGraph:
    """Build a synthetic graph with 2 repos, cross-repo edges, and API endpoints."""
    graph = CodeGraph()

    # --- Repo A: a web service ---
    graph.add_node(Node(
        id="repo-a:", repo="repo-a", kind=NodeKind.REPOSITORY, name="repo-a",
    ))
    graph.add_node(Node(
        id="repo-a:app.py", repo="repo-a", kind=NodeKind.FILE,
        name="app.py", path="app.py",
    ))
    graph.add_node(Node(
        id="repo-a:models.py", repo="repo-a", kind=NodeKind.FILE,
        name="models.py", path="models.py",
    ))
    graph.add_node(Node(
        id="repo-a:app.py::create_order", repo="repo-a",
        kind=NodeKind.API_ENDPOINT, name="create_order",
        path="app.py", start_line=10, end_line=20,
        raw_code='@app.post("/api/orders")\ndef create_order():\n    pass',
        signature="def create_order()",
    ))
    graph.add_node(Node(
        id="repo-a:app.py::get_users", repo="repo-a",
        kind=NodeKind.API_ENDPOINT, name="get_users",
        path="app.py", start_line=25, end_line=35,
        raw_code='@app.get("/api/users")\ndef get_users():\n    pass',
        signature="def get_users()",
    ))
    graph.add_node(Node(
        id="repo-a:app.py::helper", repo="repo-a",
        kind=NodeKind.FUNCTION, name="helper",
        path="app.py", start_line=40, end_line=45,
    ))
    # Flask import node
    graph.add_node(Node(
        id="repo-a:flask", repo="repo-a", kind=NodeKind.MODULE, name="flask",
    ))
    graph.add_edge(Edge(
        source="repo-a:app.py", target="repo-a:flask", kind=EdgeKind.IMPORTS,
    ))

    # --- Repo B: a library ---
    graph.add_node(Node(
        id="repo-b:", repo="repo-b", kind=NodeKind.REPOSITORY, name="repo-b",
    ))
    graph.add_node(Node(
        id="repo-b:lib.py", repo="repo-b", kind=NodeKind.FILE,
        name="lib.py", path="lib.py",
    ))
    graph.add_node(Node(
        id="repo-b:utils.go", repo="repo-b", kind=NodeKind.FILE,
        name="utils.go", path="utils.go",
    ))
    graph.add_node(Node(
        id="repo-b:lib.py::process", repo="repo-b",
        kind=NodeKind.FUNCTION, name="process",
        path="lib.py", start_line=1, end_line=10,
    ))

    # --- Cross-repo edges ---
    # repo-b calls repo-a's API
    graph.add_edge(Edge(
        source="repo-b:lib.py::process",
        target="repo-a:app.py::create_order",
        kind=EdgeKind.API_CALLS,
    ))
    # repo-a uses repo-b as shared lib
    graph.add_edge(Edge(
        source="repo-a:app.py::helper",
        target="repo-b:lib.py::process",
        kind=EdgeKind.SHARED_LIB,
    ))

    # Containment edges
    graph.add_edge(Edge(
        source="repo-a:", target="repo-a:app.py", kind=EdgeKind.CONTAINS,
    ))
    graph.add_edge(Edge(
        source="repo-a:", target="repo-a:models.py", kind=EdgeKind.CONTAINS,
    ))
    graph.add_edge(Edge(
        source="repo-b:", target="repo-b:lib.py", kind=EdgeKind.CONTAINS,
    ))
    graph.add_edge(Edge(
        source="repo-b:", target="repo-b:utils.go", kind=EdgeKind.CONTAINS,
    ))

    return graph


@pytest.fixture
def graph():
    return _build_test_graph()


@pytest.fixture
def catalog_service():
    return CatalogService(quality_service=QualityService())


class TestGenerateCatalog:
    def test_returns_all_repos(self, catalog_service, graph):
        result = catalog_service.generate_catalog(graph)
        repos = [s.repo for s in result.services]
        assert "repo-a" in repos
        assert "repo-b" in repos
        assert len(result.services) == 2

    def test_repo_filter(self, catalog_service, graph):
        result = catalog_service.generate_catalog(graph, repos=["repo-a"])
        assert len(result.services) == 1
        assert result.services[0].repo == "repo-a"

    def test_api_discovery(self, catalog_service, graph):
        result = catalog_service.generate_catalog(graph, repos=["repo-a"])
        entry = result.services[0]
        assert len(entry.apis) == 2
        paths = {a.path for a in entry.apis}
        assert "/api/orders" in paths
        assert "/api/users" in paths
        methods = {a.method for a in entry.apis}
        assert "POST" in methods
        assert "GET" in methods

    def test_no_apis_in_library(self, catalog_service, graph):
        result = catalog_service.generate_catalog(graph, repos=["repo-b"])
        entry = result.services[0]
        assert len(entry.apis) == 0

    def test_cross_repo_dependencies(self, catalog_service, graph):
        result = catalog_service.generate_catalog(graph)
        repo_a = next(s for s in result.services if s.repo == "repo-a")
        repo_b = next(s for s in result.services if s.repo == "repo-b")

        # repo-a depends on repo-b (shared_lib edge)
        assert len(repo_a.dependencies) >= 1
        dep_targets = {d.target_repo for d in repo_a.dependencies}
        assert "repo-b" in dep_targets

        # repo-b depends on repo-a (api_calls edge)
        assert len(repo_b.dependencies) >= 1
        dep_targets = {d.target_repo for d in repo_b.dependencies}
        assert "repo-a" in dep_targets

    def test_dependents(self, catalog_service, graph):
        result = catalog_service.generate_catalog(graph)
        repo_a = next(s for s in result.services if s.repo == "repo-a")

        # repo-a is depended on by repo-b (api_calls)
        dep_sources = {d.source_repo for d in repo_a.dependents}
        assert "repo-b" in dep_sources

    def test_tech_stack_languages(self, catalog_service, graph):
        result = catalog_service.generate_catalog(graph)
        repo_a = next(s for s in result.services if s.repo == "repo-a")
        assert "python" in repo_a.tech_stack.languages

        repo_b = next(s for s in result.services if s.repo == "repo-b")
        assert "python" in repo_b.tech_stack.languages
        assert "go" in repo_b.tech_stack.languages

    def test_tech_stack_frameworks(self, catalog_service, graph):
        result = catalog_service.generate_catalog(graph)
        repo_a = next(s for s in result.services if s.repo == "repo-a")
        assert "Flask" in repo_a.tech_stack.frameworks

    def test_node_counts(self, catalog_service, graph):
        result = catalog_service.generate_catalog(graph)
        repo_a = next(s for s in result.services if s.repo == "repo-a")
        assert repo_a.node_counts.get("file", 0) == 2
        assert repo_a.node_counts.get("api_endpoint", 0) == 2

    def test_quality_score_in_range(self, catalog_service, graph):
        result = catalog_service.generate_catalog(graph)
        for entry in result.services:
            assert 0.0 <= entry.quality_score <= 1.0

    def test_generated_at_present(self, catalog_service, graph):
        result = catalog_service.generate_catalog(graph)
        assert result.generated_at != ""

    def test_to_dict_roundtrip(self, catalog_service, graph):
        result = catalog_service.generate_catalog(graph)
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "services" in d
        assert "generated_at" in d
        assert isinstance(d["services"], list)
        for s in d["services"]:
            assert "repo" in s
            assert "apis" in s
            assert "dependencies" in s
            assert "tech_stack" in s


class TestDetectDrift:
    def test_confirmed_dependency(self, catalog_service, graph):
        report = catalog_service.detect_drift(
            graph, "repo-a",
            declared_deps=[{"name": "repo-b", "type": "library"}],
        )
        assert len(report.confirmed) == 1
        assert report.confirmed[0].dependency == "repo-b"
        assert report.confirmed[0].status == "confirmed"

    def test_missing_in_code(self, catalog_service, graph):
        report = catalog_service.detect_drift(
            graph, "repo-a",
            declared_deps=[
                {"name": "repo-b", "type": "library"},
                {"name": "nonexistent", "type": "api"},
            ],
        )
        missing = [e for e in report.missing_in_code]
        assert len(missing) == 1
        assert missing[0].dependency == "nonexistent"

    def test_undeclared_dependency(self, catalog_service, graph):
        report = catalog_service.detect_drift(
            graph, "repo-a",
            declared_deps=[],  # declare nothing
        )
        # repo-a actually depends on repo-b via shared_lib
        undeclared = [e for e in report.undeclared]
        assert len(undeclared) >= 1
        undeclared_names = {e.dependency for e in undeclared}
        assert "repo-b" in undeclared_names

    def test_perfect_drift_score(self, catalog_service, graph):
        report = catalog_service.detect_drift(
            graph, "repo-a",
            declared_deps=[{"name": "repo-b", "type": "library"}],
        )
        assert report.drift_score == 0.0

    def test_full_drift_score(self, catalog_service, graph):
        report = catalog_service.detect_drift(
            graph, "repo-a",
            declared_deps=[{"name": "fake-service", "type": "api"}],
        )
        # 1 missing + 1 undeclared, 0 confirmed → drift_score = 1.0
        assert report.drift_score == 1.0

    def test_drift_report_to_dict(self, catalog_service, graph):
        report = catalog_service.detect_drift(
            graph, "repo-a",
            declared_deps=[{"name": "repo-b", "type": "library"}],
        )
        d = report.to_dict()
        assert "repo" in d
        assert "confirmed" in d
        assert "missing_in_code" in d
        assert "undeclared" in d
        assert "drift_score" in d


class TestOwnership:
    def test_owner_defaults_to_unknown_without_config(self, catalog_service, graph):
        result = catalog_service.generate_catalog(graph, repos=["repo-a"])
        assert result.services[0].owner == "unknown"

    def test_owner_inferred_from_git(self, graph):
        from mimir.domain.config import MimirConfig, RepoConfig
        from pathlib import Path

        # Mock a config with repo paths
        mock_repo_config = MagicMock(spec=RepoConfig)
        mock_repo_config.name = "repo-a"
        mock_repo_config.path = Path("/fake/repo-a")

        mock_config = MagicMock(spec=MimirConfig)
        mock_config.repos = [mock_repo_config]

        service = CatalogService(quality_service=QualityService(), config=mock_config)

        # Mock git to return commits with known authors
        mock_commit_1 = MagicMock()
        mock_commit_1.author.email = "alice@example.com"
        mock_commit_2 = MagicMock()
        mock_commit_2.author.email = "alice@example.com"
        mock_commit_3 = MagicMock()
        mock_commit_3.author.email = "bob@example.com"

        mock_repo = MagicMock()
        mock_repo.iter_commits.return_value = [mock_commit_1, mock_commit_2, mock_commit_3]

        mock_git = MagicMock()
        mock_git.Repo.return_value = mock_repo

        with patch.dict("sys.modules", {"git": mock_git}):
            result = service.generate_catalog(graph, repos=["repo-a"])

        assert result.services[0].owner == "user:alice@example.com"

    def test_to_dict_includes_owner(self, catalog_service, graph):
        result = catalog_service.generate_catalog(graph, repos=["repo-a"])
        d = result.to_dict()
        assert "owner" in d["services"][0]


class TestFormatForLlm:
    def test_catalog_format_for_llm(self, catalog_service, graph):
        result = catalog_service.generate_catalog(graph)
        text = result.format_for_llm()
        assert "Service Catalog" in text
        assert "repo-a" in text
        assert "repo-b" in text
        assert "service" in text.lower()

    def test_drift_format_for_llm(self, catalog_service, graph):
        report = catalog_service.detect_drift(
            graph, "repo-a",
            declared_deps=[
                {"name": "repo-b", "type": "library"},
                {"name": "nonexistent", "type": "api"},
            ],
        )
        text = report.format_for_llm()
        assert "Drift Report" in text
        assert "repo-a" in text
        assert "Confirmed" in text
        assert "repo-b" in text
        assert "nonexistent" in text


class TestContractShape:
    """Verify the JSON output shape matches what the TypeScript plugin expects."""

    def test_catalog_response_shape(self, catalog_service, graph):
        result = catalog_service.generate_catalog(graph)
        d = result.to_dict()

        # JSON round-trip must succeed
        assert json.loads(json.dumps(d)) == d

        # Top-level keys
        assert isinstance(d["services"], list)
        assert isinstance(d["generated_at"], str)

        for svc in d["services"]:
            # Required service keys per types.ts MimirServiceEntry
            assert isinstance(svc["repo"], str)
            assert isinstance(svc["node_id"], str)
            assert isinstance(svc["owner"], str)
            assert isinstance(svc["apis"], list)
            assert isinstance(svc["dependencies"], list)
            assert isinstance(svc["dependents"], list)
            assert isinstance(svc["tech_stack"], dict)
            assert isinstance(svc["quality_score"], (int, float))
            assert isinstance(svc["quality_distribution"], dict)
            assert isinstance(svc["node_counts"], dict)

            # Tech stack shape per types.ts MimirTechStack
            ts = svc["tech_stack"]
            assert isinstance(ts["languages"], dict)
            assert isinstance(ts["frameworks"], list)
            assert isinstance(ts["key_dependencies"], list)

            # API shape per types.ts MimirCatalogApi
            for api in svc["apis"]:
                assert isinstance(api["node_id"], str)
                assert isinstance(api["path"], str)
                assert isinstance(api["method"], str)
                assert isinstance(api["containing_function"], str)
                assert isinstance(api["repo"], str)

            # Dependency shape per types.ts MimirServiceDependency
            for dep in svc["dependencies"] + svc["dependents"]:
                assert isinstance(dep["source_repo"], str)
                assert isinstance(dep["target_repo"], str)
                assert isinstance(dep["dependency_type"], str)
                assert isinstance(dep["evidence"], list)
