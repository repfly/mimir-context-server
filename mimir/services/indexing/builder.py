"""Graph-construction helpers for the indexing pipeline."""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from pathlib import Path
from typing import Optional

from mimir.domain.config import MimirConfig
from mimir.domain.errors import ParsingError
from mimir.domain.graph import CodeGraph
from mimir.domain.models import Edge, EdgeKind, Node, NodeKind
from mimir.ports.parser import Parser, Symbol
from mimir.services.indexing.refs import resolve_affected_refs
from mimir.services.summarizer import heuristic_summary

logger = logging.getLogger(__name__)


class IndexingGraphBuilder:
    """Owns repository/file parsing and graph construction details."""

    _git_file_cache: dict[str, dict] = {}

    def __init__(self, config: MimirConfig, parser: Parser) -> None:
        self._config = config
        self._parser = parser

    async def index_repo(self, graph: CodeGraph, repo_config) -> None:
        repo_name = repo_config.name
        repo_path = Path(repo_config.path)
        language = repo_config.language_hint

        repo_node = Node(
            id=f"{repo_name}:",
            repo=repo_name,
            kind=NodeKind.REPOSITORY,
            name=repo_name,
            path=str(repo_path),
        )
        graph.add_node(repo_node)

        files_indexed = 0
        symbols_indexed = 0

        for root, dirs, files in os.walk(str(repo_path)):
            dirs[:] = [d for d in dirs if not self.is_excluded(d)]

            rel_dir = os.path.relpath(root, str(repo_path))
            if rel_dir == ".":
                rel_dir = ""

            for filename in files:
                if self.is_excluded(filename):
                    continue

                file_path = os.path.join(root, filename)
                rel_path = os.path.relpath(file_path, str(repo_path))

                try:
                    size_kb = os.path.getsize(file_path) / 1024
                    if size_kb > self._config.indexing.max_file_size_kb:
                        logger.debug("Skipping large file: %s (%.0f KB)", rel_path, size_kb)
                        continue
                except OSError:
                    continue

                try:
                    symbols = await self._parser.parse_file(file_path, language)
                except ParsingError as exc:
                    logger.warning("Parse failed: %s", exc)
                    continue
                except Exception as exc:
                    logger.warning("Unexpected parse error for %s: %s", rel_path, exc)
                    continue

                if not symbols:
                    continue

                file_id = f"{repo_name}:{rel_path}"
                file_node = Node(
                    id=file_id,
                    repo=repo_name,
                    kind=NodeKind.FILE,
                    name=filename,
                    path=rel_path,
                )
                graph.add_node(file_node)
                files_indexed += 1

                parent_id, _, _ = self.ensure_module_hierarchy(
                    graph,
                    repo_name,
                    rel_dir,
                    repo_root_path=repo_path,
                )
                graph.add_edge(Edge(
                    source=parent_id,
                    target=file_id,
                    kind=EdgeKind.CONTAINS,
                ))

                for sym in symbols:
                    self.add_symbol_to_graph(
                        graph,
                        sym,
                        repo_name,
                        rel_path,
                        file_id,
                        repo_path,
                    )
                    symbols_indexed += 1

        logger.info("Repo %s: %d files, %d symbols", repo_name, files_indexed, symbols_indexed)

    async def index_files(
        self,
        graph: CodeGraph,
        repo_name: str,
        repo_path: Path,
        changed_files: set[str],
        deleted_files: set[str],
        *,
        language_hint: Optional[str] = None,
    ) -> tuple[list[str], list[Node], list[Edge]]:
        removed_ids: list[str] = []
        new_nodes: list[Node] = []
        new_edges: list[Edge] = []

        files_to_remove = deleted_files | changed_files
        if files_to_remove:
            removed_ids = graph.remove_nodes_by_paths(repo_name, files_to_remove)

        for rel_path in sorted(changed_files):
            file_path = str(repo_path / rel_path)

            if self.is_excluded(os.path.basename(rel_path)):
                continue
            if not os.path.isfile(file_path):
                continue

            try:
                size_kb = os.path.getsize(file_path) / 1024
                if size_kb > self._config.indexing.max_file_size_kb:
                    continue
            except OSError:
                continue

            try:
                symbols = await self._parser.parse_file(file_path, language_hint)
            except Exception as exc:
                logger.warning("Watcher parse failed for %s: %s", rel_path, exc)
                continue

            if not symbols:
                continue

            file_id = f"{repo_name}:{rel_path}"
            file_node = Node(
                id=file_id,
                repo=repo_name,
                kind=NodeKind.FILE,
                name=os.path.basename(rel_path),
                path=rel_path,
            )
            graph.add_node(file_node)
            new_nodes.append(file_node)

            rel_dir = os.path.dirname(rel_path)
            parent_id, module_nodes, module_edges = self.ensure_module_hierarchy(
                graph,
                repo_name,
                rel_dir,
                repo_root_path=repo_path,
            )
            new_nodes.extend(module_nodes)
            new_edges.extend(module_edges)
            edge = Edge(source=parent_id, target=file_id, kind=EdgeKind.CONTAINS)
            graph.add_edge(edge)
            new_edges.append(edge)

            for sym in symbols:
                node, edges = self.add_symbol_to_graph(
                    graph,
                    sym,
                    repo_name,
                    rel_path,
                    file_id,
                    repo_path,
                    resolve_edges=False,
                )
                new_nodes.append(node)
                new_edges.extend(edges)

        new_symbol_nodes = [node for node in new_nodes if node.is_symbol]
        if new_symbol_nodes and hasattr(self._parser, "extract_identifiers"):
            xref_edges = resolve_affected_refs(graph, new_symbol_nodes, self._parser)
            new_edges.extend(xref_edges)

        for node in new_nodes:
            node.summary = heuristic_summary(node, graph)

        logger.info(
            "index_files: -%d removed, +%d nodes, +%d edges",
            len(removed_ids),
            len(new_nodes),
            len(new_edges),
        )
        return removed_ids, new_nodes, new_edges

    def is_excluded(self, name: str) -> bool:
        return any(fnmatch.fnmatch(name, pattern) for pattern in self._config.indexing.excluded_patterns)

    @staticmethod
    def map_symbol_kind(kind_str: str) -> NodeKind:
        mapping = {
            "function": NodeKind.FUNCTION,
            "method": NodeKind.METHOD,
            "class": NodeKind.CLASS,
            "type": NodeKind.TYPE,
            "constant": NodeKind.CONSTANT,
        }
        return mapping.get(kind_str, NodeKind.FUNCTION)

    def ensure_module_hierarchy(
        self,
        graph: CodeGraph,
        repo_name: str,
        rel_dir: str,
        *,
        repo_root_path: Optional[Path] = None,
    ) -> tuple[str, list[Node], list[Edge]]:
        created_nodes: list[Node] = []
        created_edges: list[Edge] = []
        repo_id = f"{repo_name}:"
        if not graph.has_node(repo_id):
            repo_node = Node(
                id=repo_id,
                repo=repo_name,
                kind=NodeKind.REPOSITORY,
                name=repo_name,
                path=str(repo_root_path) if repo_root_path is not None else None,
            )
            graph.add_node(repo_node)
            created_nodes.append(repo_node)

        if not rel_dir:
            return repo_id, created_nodes, created_edges

        current_parent = repo_id
        current_parts: list[str] = []
        for part in Path(rel_dir).parts:
            current_parts.append(part)
            module_path = "/".join(current_parts)
            module_id = f"{repo_name}:{module_path}/"
            if not graph.has_node(module_id):
                module_node = Node(
                    id=module_id,
                    repo=repo_name,
                    kind=NodeKind.MODULE,
                    name=part,
                    path=module_path,
                )
                graph.add_node(module_node)
                created_nodes.append(module_node)
                edge = Edge(source=current_parent, target=module_id, kind=EdgeKind.CONTAINS)
                graph.add_edge(edge)
                created_edges.append(edge)
            current_parent = module_id

        return current_parent, created_nodes, created_edges

    def add_symbol_to_graph(
        self,
        graph: CodeGraph,
        sym: Symbol,
        repo_name: str,
        rel_path: str,
        file_id: str,
        repo_path: Path,
        *,
        resolve_edges: bool = True,
    ) -> tuple[Node, list[Edge]]:
        base_id = f"{repo_name}:{rel_path}::{sym.name}"
        sym_id = base_id
        suffix = 2
        while graph.has_node(sym_id):
            sym_id = f"{base_id}_{suffix}"
            suffix += 1

        sym_node = Node(
            id=sym_id,
            repo=repo_name,
            kind=self.map_symbol_kind(sym.kind),
            name=sym.name,
            path=rel_path,
            start_line=sym.start_line,
            end_line=sym.end_line,
            raw_code=sym.code,
            signature=sym.signature,
            docstring=sym.docstring,
        )
        self.populate_git_metadata(sym_node, repo_path)
        graph.add_node(sym_node)

        edges: list[Edge] = []
        contains = Edge(source=file_id, target=sym_id, kind=EdgeKind.CONTAINS)
        graph.add_edge(contains)
        edges.append(contains)

        if resolve_edges:
            for callee_name in sym.calls:
                callee_id = self.resolve_symbol(callee_name, repo_name, graph)
                if callee_id:
                    edge = Edge(source=sym_id, target=callee_id, kind=EdgeKind.CALLS)
                    graph.add_edge(edge)
                    edges.append(edge)

            for import_name in sym.imports:
                import_id = self.resolve_symbol(import_name, repo_name, graph)
                if import_id:
                    edge = Edge(source=sym_id, target=import_id, kind=EdgeKind.IMPORTS)
                    graph.add_edge(edge)
                    edges.append(edge)

            for decorator in sym.decorators:
                endpoint_info = self.parse_endpoint_decorator(decorator)
                if endpoint_info:
                    current = graph.get_node(sym_id)
                    if current:
                        api_node = Node(
                            id=sym_id,
                            repo=repo_name,
                            kind=NodeKind.API_ENDPOINT,
                            name=sym.name,
                            path=rel_path,
                            start_line=sym.start_line,
                            end_line=sym.end_line,
                            raw_code=sym.code,
                            signature=sym.signature,
                            docstring=sym.docstring,
                            last_modified=current.last_modified,
                            modification_count=current.modification_count,
                            http_method=endpoint_info["method"],
                            route_path=endpoint_info["endpoint"],
                        )
                        graph.add_node(api_node)
                        sym_node = api_node

        return sym_node, edges

    @staticmethod
    def resolve_symbol(name: str, repo_name: str, graph: CodeGraph) -> Optional[str]:
        for node in graph.nodes_by_repo(repo_name):
            if node.name == name and node.is_symbol:
                return node.id
        return None

    @classmethod
    def populate_git_metadata(cls, node: Node, repo_path: Path) -> None:
        cache_key = f"{repo_path}:{node.path}"
        cached = cls._git_file_cache.get(cache_key)
        if cached is not None:
            node.last_modified = cached.get("last_modified")
            node.modification_count = cached.get("modification_count", 0)
            return

        try:
            import git

            repo = git.Repo(str(repo_path))
            if node.path:
                commits = list(repo.iter_commits(paths=str(node.path), max_count=50))
                if commits:
                    info = {
                        "last_modified": commits[0].committed_datetime.isoformat(),
                        "modification_count": len(commits),
                    }
                    cls._git_file_cache[cache_key] = info
                    node.last_modified = info["last_modified"]
                    node.modification_count = info["modification_count"]
                    return
        except Exception:
            pass

        cls._git_file_cache[cache_key] = {}

    @staticmethod
    def parse_endpoint_decorator(decorator: str) -> Optional[dict]:
        patterns = [
            r'@\w+\.(get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)',
            r'@app\.route\s*\(\s*["\']([^"\']+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, decorator, re.IGNORECASE)
            if not match:
                continue
            groups = match.groups()
            if len(groups) == 2:
                return {"method": groups[0].upper(), "endpoint": groups[1]}
            return {"method": "GET", "endpoint": groups[0]}
        return None
