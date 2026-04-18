"""Tests for the DiffAnalyzer service."""

from __future__ import annotations

import textwrap
from unittest.mock import AsyncMock

import pytest

from mimir.domain.graph import CodeGraph
from mimir.domain.guardrails import ChangeSet
from mimir.domain.models import Edge, EdgeKind, Node, NodeKind
from mimir.services.guardrail.diff_analyzer import DiffAnalyzer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_test_graph() -> CodeGraph:
    """Build a synthetic graph for diff analysis tests."""
    graph = CodeGraph()

    # Repo node
    graph.add_node(Node(
        id="myrepo:", repo="myrepo", kind=NodeKind.REPOSITORY, name="myrepo",
    ))

    # File node
    graph.add_node(Node(
        id="myrepo:src/service.py", repo="myrepo", kind=NodeKind.FILE,
        name="service.py", path="src/service.py",
    ))

    # Function nodes with line ranges
    graph.add_node(Node(
        id="myrepo:src/service.py::handle_request", repo="myrepo",
        kind=NodeKind.FUNCTION, name="handle_request",
        path="src/service.py", start_line=10, end_line=25,
        raw_code="def handle_request(): ...",
    ))
    graph.add_node(Node(
        id="myrepo:src/service.py::validate_input", repo="myrepo",
        kind=NodeKind.FUNCTION, name="validate_input",
        path="src/service.py", start_line=30, end_line=45,
        raw_code="def validate_input(): ...",
    ))
    graph.add_node(Node(
        id="myrepo:src/service.py::helper", repo="myrepo",
        kind=NodeKind.FUNCTION, name="helper",
        path="src/service.py", start_line=50, end_line=60,
    ))

    # Second file
    graph.add_node(Node(
        id="myrepo:src/models.py", repo="myrepo", kind=NodeKind.FILE,
        name="models.py", path="src/models.py",
    ))
    graph.add_node(Node(
        id="myrepo:src/models.py::User", repo="myrepo",
        kind=NodeKind.CLASS, name="User",
        path="src/models.py", start_line=5, end_line=20,
    ))

    # Existing edges
    graph.add_edge(Edge(
        source="myrepo:src/service.py",
        target="myrepo:src/models.py",
        kind=EdgeKind.IMPORTS,
    ))

    return graph


def _make_parser_mock() -> AsyncMock:
    """Create a mock Parser that returns empty results."""
    parser = AsyncMock()
    parser.parse_file = AsyncMock(return_value=[])
    parser.supported_extensions.return_value = frozenset({".py"})
    return parser


# ---------------------------------------------------------------------------
# Diff parsing tests
# ---------------------------------------------------------------------------


SAMPLE_DIFF_MODIFIED = textwrap.dedent("""\
    diff --git a/src/service.py b/src/service.py
    index abc1234..def5678 100644
    --- a/src/service.py
    +++ b/src/service.py
    @@ -10,5 +10,7 @@ some context
     def handle_request():
    -    old_code()
    +    new_code()
    +    another_line()
         pass
    @@ -30,3 +32,4 @@ more context
     def validate_input():
    +    import os
         pass
""")

SAMPLE_DIFF_NEW_FILE = textwrap.dedent("""\
    diff --git a/src/new_module.py b/src/new_module.py
    new file mode 100644
    index 0000000..abc1234
    --- /dev/null
    +++ b/src/new_module.py
    @@ -0,0 +1,5 @@
    +def new_function():
    +    pass
    +
    +def another():
    +    return 42
""")

SAMPLE_DIFF_DELETED = textwrap.dedent("""\
    diff --git a/src/models.py b/src/models.py
    deleted file mode 100644
    index abc1234..0000000
    --- a/src/models.py
    +++ /dev/null
    @@ -1,20 +0,0 @@
    -class User:
    -    pass
""")

SAMPLE_DIFF_WITH_IMPORT = textwrap.dedent("""\
    diff --git a/src/service.py b/src/service.py
    index abc1234..def5678 100644
    --- a/src/service.py
    +++ b/src/service.py
    @@ -1,3 +1,4 @@
    +from src.infra.db import Database
     def handle_request():
         pass
""")


class TestDiffParsing:
    async def test_parse_modified_file(self):
        parser = _make_parser_mock()
        analyzer = DiffAnalyzer(parser)
        graph = _build_test_graph()

        result = await analyzer.analyze(graph, SAMPLE_DIFF_MODIFIED)

        assert isinstance(result, ChangeSet)
        assert "src/service.py" in result.affected_files

    async def test_modified_maps_to_nodes(self):
        parser = _make_parser_mock()
        analyzer = DiffAnalyzer(parser)
        graph = _build_test_graph()

        result = await analyzer.analyze(graph, SAMPLE_DIFF_MODIFIED)

        # Changes at lines 10-16 should hit handle_request (10-25)
        # Changes at lines 32-35 should hit validate_input (30-45)
        assert "myrepo:src/service.py::handle_request" in result.modified_nodes
        assert "myrepo:src/service.py::validate_input" in result.modified_nodes
        # helper (50-60) should NOT be matched
        assert "myrepo:src/service.py::helper" not in result.modified_nodes

    async def test_new_file(self):
        parser = _make_parser_mock()
        analyzer = DiffAnalyzer(parser)
        graph = _build_test_graph()

        result = await analyzer.analyze(graph, SAMPLE_DIFF_NEW_FILE)

        assert "src/new_module.py" in result.affected_files

    async def test_deleted_file(self):
        parser = _make_parser_mock()
        analyzer = DiffAnalyzer(parser)
        graph = _build_test_graph()

        result = await analyzer.analyze(graph, SAMPLE_DIFF_DELETED)

        assert "src/models.py" in result.affected_files
        # Nodes from the deleted file should be in modified_nodes
        assert "myrepo:src/models.py" in result.modified_nodes or \
               "myrepo:src/models.py::User" in result.modified_nodes

    async def test_empty_diff(self):
        parser = _make_parser_mock()
        analyzer = DiffAnalyzer(parser)
        graph = _build_test_graph()

        result = await analyzer.analyze(graph, "")
        assert result == ChangeSet()

        result = await analyzer.analyze(graph, "   \n  ")
        assert result == ChangeSet()

    async def test_import_detection(self):
        parser = _make_parser_mock()
        analyzer = DiffAnalyzer(parser)
        graph = _build_test_graph()

        result = await analyzer.analyze(graph, SAMPLE_DIFF_WITH_IMPORT)

        # Should detect the new import edge
        assert len(result.new_edges) > 0
        import_targets = [e.target for e in result.new_edges if e.kind == EdgeKind.IMPORTS]
        assert "src.infra.db" in import_targets


class TestImportExtraction:
    def test_python_import(self):
        lines = ["import os", "from pathlib import Path"]
        targets = DiffAnalyzer._extract_import_targets(lines)
        assert "os" in targets
        assert "pathlib" in targets

    def test_js_import(self):
        lines = ['import { foo } from "bar"', "import x from 'baz'"]
        targets = DiffAnalyzer._extract_import_targets(lines)
        assert "bar" in targets
        assert "baz" in targets

    def test_no_imports(self):
        lines = ["x = 1", "def foo(): pass"]
        targets = DiffAnalyzer._extract_import_targets(lines)
        assert targets == []


class TestMultiFileDiff:
    async def test_multiple_files(self):
        diff = SAMPLE_DIFF_MODIFIED + "\n" + SAMPLE_DIFF_WITH_IMPORT
        parser = _make_parser_mock()
        analyzer = DiffAnalyzer(parser)
        graph = _build_test_graph()

        # This should parse without error even with duplicate file
        result = await analyzer.analyze(graph, diff)
        assert "src/service.py" in result.affected_files
