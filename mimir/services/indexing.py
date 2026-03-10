"""Indexing service — orchestrates parsing, graph building, summarization, and embedding.

This is the primary application service for Milestone 1+2.
It receives all infrastructure dependencies via constructor injection.
"""

from __future__ import annotations

import fnmatch
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mimir.domain.config import MimirConfig
from mimir.domain.errors import IndexingError, ParsingError
from mimir.domain.graph import CodeGraph
from mimir.domain.models import Edge, EdgeKind, Node, NodeKind
from mimir.ports.embedder import Embedder
from mimir.ports.graph_store import GraphStore
from mimir.ports.llm_client import LlmClient
from mimir.domain.lang import detect_language
from mimir.ports.parser import Parser, Symbol
from mimir.ports.vector_store import VectorStore

logger = logging.getLogger(__name__)


class IndexingService:
    """Orchestrates the full indexing pipeline."""

    def __init__(
        self,
        config: MimirConfig,
        parser: Parser,
        embedder: Embedder,
        vector_store: VectorStore,
        graph_store: GraphStore,
        llm_client: Optional[LlmClient] = None,
    ) -> None:
        self._config = config
        self._parser = parser
        self._embedder = embedder
        self._vector_store = vector_store
        self._graph_store = graph_store
        self._llm_client = llm_client

    async def index_all(self, *, mode_override: Optional[str] = None) -> CodeGraph:
        """Run the full indexing pipeline.

        1. Parse all repos → build CodeGraph
        2. Cross-file symbol resolution (CALLS, USES_TYPE, INHERITS edges)
        3. Cross-repo link detection (API contracts, shared imports)
        4. Generate summaries (based on mode)
        5. Embed nodes
        6. Store in vector DB and SQLite
        """
        mode = mode_override or self._config.indexing.summary_mode
        logger.info("Starting full index — mode=%s, repos=%d", mode, len(self._config.repos))

        graph = CodeGraph()

        # Phase 1: Parse all repos
        for repo_config in self._config.repos:
            logger.info("Indexing repo: %s (%s)", repo_config.name, repo_config.path)
            await self._index_repo(graph, repo_config)

        # Phase 2: Cross-file symbol resolution
        self._resolve_cross_file_refs(graph)

        # Phase 3: Cross-repo link detection
        if self._config.cross_repo.detect_api_contracts:
            self._detect_api_contracts(graph)
        if self._config.cross_repo.detect_shared_imports:
            self._detect_shared_imports(graph)

        logger.info("Graph built: %s", graph.stats())

        # Phase 4: Summarization (mode-dependent)
        if mode == "heuristic":
            self._generate_heuristic_summaries(graph)
        elif mode == "llm":
            if self._llm_client is None:
                raise IndexingError("LLM client required for llm mode but not configured")
            await self._generate_llm_summaries(graph)

        # Phase 5: Embedding
        await self._embed_graph(graph, mode)

        # Phase 6: Persist
        self._graph_store.save(graph)

        # Save commit hashes for incremental updates
        for repo_config in self._config.repos:
            try:
                import git
                repo = git.Repo(str(repo_config.path))
                commit_hash = repo.head.commit.hexsha
                self._graph_store.save_repo_state(repo_config.name, commit_hash)
            except Exception:
                logger.debug("Could not read git commit for %s", repo_config.name)

        logger.info("Indexing complete: %d nodes, %d edges", graph.node_count, graph.edge_count)
        return graph

    async def index_incremental(self, *, mode_override: Optional[str] = None) -> tuple[CodeGraph, dict]:
        """Incremental index — only re-process files changed since the last commit.

        Flow:
        1. Load existing graph from storage
        2. For each repo, compute git diff against last-indexed commit
        3. Remove stale nodes (deleted & modified files)
        4. Re-parse only changed/added files
        5. Regenerate summaries & embeddings for affected nodes
        6. Persist delta to storage

        Returns (graph, report) where report is a dict summarising the changes.
        """
        mode = mode_override or self._config.indexing.summary_mode
        logger.info("Starting incremental index — mode=%s, repos=%d", mode, len(self._config.repos))

        # Load existing graph
        graph = self._graph_store.load()
        if graph.node_count == 0:
            logger.info("No existing graph found — falling back to full index")
            return await self.index_all(mode_override=mode_override), {
                "mode": "full_fallback",
                "reason": "no existing graph",
            }

        # Get stored commit hashes
        stored_states = self._graph_store.get_all_repo_states()

        report: dict = {
            "mode": "incremental",
            "repos": {},
        }

        all_new_nodes: list[Node] = []
        all_new_edges: list[Edge] = []
        all_stale_ids: list[str] = []
        repos_needing_full: list = []

        for repo_config in self._config.repos:
            repo_name = repo_config.name
            repo_path = Path(repo_config.path)
            last_commit = stored_states.get(repo_name)

            if not last_commit:
                logger.info("Repo %s: no previous commit — will full-index", repo_name)
                repos_needing_full.append(repo_config)
                report["repos"][repo_name] = {"status": "full_index", "reason": "first time"}
                continue

            # Compute diff
            try:
                import git as gitmodule
                repo = gitmodule.Repo(str(repo_path))
                current_commit = repo.head.commit.hexsha
            except Exception as exc:
                logger.warning("Cannot read git for %s: %s — skipping", repo_name, exc)
                report["repos"][repo_name] = {"status": "skipped", "reason": str(exc)}
                continue

            if current_commit == last_commit:
                logger.info("Repo %s: no changes (still at %s)", repo_name, current_commit[:8])
                report["repos"][repo_name] = {"status": "up_to_date", "commit": current_commit[:8]}
                continue

            # Get the actual diff
            try:
                diff = repo.commit(last_commit).diff(current_commit)
            except Exception as exc:
                logger.warning(
                    "Repo %s: diff failed (%s) — falling back to full index for this repo",
                    repo_name, exc,
                )
                repos_needing_full.append(repo_config)
                report["repos"][repo_name] = {"status": "full_index", "reason": f"diff failed: {exc}"}
                continue

            # Categorise changed files
            added_files: set[str] = set()
            modified_files: set[str] = set()
            deleted_files: set[str] = set()

            for d in diff:
                if d.new_file:
                    if d.b_path:
                        added_files.add(d.b_path)
                elif d.deleted_file:
                    if d.a_path:
                        deleted_files.add(d.a_path)
                elif d.renamed_file:
                    if d.a_path:
                        deleted_files.add(d.a_path)
                    if d.b_path:
                        added_files.add(d.b_path)
                else:
                    # Modified
                    if d.b_path:
                        modified_files.add(d.b_path)

            # Filter to only parseable files (skip binary, excluded, etc.)
            files_to_remove = deleted_files | modified_files
            files_to_parse = added_files | modified_files

            logger.info(
                "Repo %s: %d added, %d modified, %d deleted (commit %s → %s)",
                repo_name,
                len(added_files),
                len(modified_files),
                len(deleted_files),
                last_commit[:8],
                current_commit[:8],
            )

            # Phase 1: Remove stale nodes from graph & store
            if files_to_remove:
                removed_ids = graph.remove_nodes_by_paths(repo_name, files_to_remove)
                all_stale_ids.extend(removed_ids)
                logger.info("Repo %s: removed %d stale nodes", repo_name, len(removed_ids))

            # Phase 2: Re-parse changed/added files into the graph
            files_parsed = 0
            symbols_parsed = 0
            for rel_path in sorted(files_to_parse):
                file_path = str(repo_path / rel_path)

                # Skip excluded files
                if self._is_excluded(os.path.basename(rel_path)):
                    continue

                # Skip files that don't exist (could be in gitignore or binary)
                if not os.path.isfile(file_path):
                    continue

                # Check file size
                try:
                    size_kb = os.path.getsize(file_path) / 1024
                    if size_kb > self._config.indexing.max_file_size_kb:
                        continue
                except OSError:
                    continue

                # Parse file
                try:
                    symbols = await self._parser.parse_file(
                        file_path, repo_config.language_hint,
                    )
                except Exception as exc:
                    logger.warning("Parse failed for %s: %s", rel_path, exc)
                    continue

                if not symbols:
                    continue

                # Create file node
                file_id = f"{repo_name}:{rel_path}"
                file_node = Node(
                    id=file_id,
                    repo=repo_name,
                    kind=NodeKind.FILE,
                    name=os.path.basename(rel_path),
                    path=rel_path,
                )
                graph.add_node(file_node)
                all_new_nodes.append(file_node)
                files_parsed += 1

                # Link file to parent module/repo
                rel_dir = os.path.dirname(rel_path)
                parent_id = f"{repo_name}:{rel_dir}/" if rel_dir else f"{repo_name}:"
                if graph.has_node(parent_id):
                    edge = Edge(source=parent_id, target=file_id, kind=EdgeKind.CONTAINS)
                    graph.add_edge(edge)
                    all_new_edges.append(edge)

                # Add symbol nodes
                for sym in symbols:
                    base_id = f"{repo_name}:{rel_path}::{sym.name}"
                    sym_id = base_id
                    suffix = 2
                    while graph.has_node(sym_id):
                        sym_id = f"{base_id}_{suffix}"
                        suffix += 1

                    node_kind = self._map_symbol_kind(sym.kind)
                    sym_node = Node(
                        id=sym_id,
                        repo=repo_name,
                        kind=node_kind,
                        name=sym.name,
                        path=rel_path,
                        start_line=sym.start_line,
                        end_line=sym.end_line,
                        raw_code=sym.code,
                        signature=sym.signature,
                        docstring=sym.docstring,
                    )
                    self._populate_git_metadata(sym_node, repo_path)
                    graph.add_node(sym_node)
                    all_new_nodes.append(sym_node)
                    symbols_parsed += 1

                    # CONTAINS edge
                    edge = Edge(source=file_id, target=sym_id, kind=EdgeKind.CONTAINS)
                    graph.add_edge(edge)
                    all_new_edges.append(edge)

                    # CALLS edges
                    for callee_name in sym.calls:
                        callee_id = self._resolve_symbol(callee_name, repo_name, rel_path, graph)
                        if callee_id:
                            call_edge = Edge(source=sym_id, target=callee_id, kind=EdgeKind.CALLS)
                            graph.add_edge(call_edge)
                            all_new_edges.append(call_edge)

                    # IMPORTS edges
                    for import_name in sym.imports:
                        import_id = self._resolve_symbol(import_name, repo_name, rel_path, graph)
                        if import_id:
                            imp_edge = Edge(source=sym_id, target=import_id, kind=EdgeKind.IMPORTS)
                            graph.add_edge(imp_edge)
                            all_new_edges.append(imp_edge)

                    # API endpoint detection
                    for dec in sym.decorators:
                        endpoint_info = self._parse_endpoint_decorator(dec)
                        if endpoint_info:
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
                                last_modified=sym_node.last_modified,
                                modification_count=sym_node.modification_count,
                            )
                            graph.add_node(api_node)
                            # Replace in new_nodes list
                            all_new_nodes = [n for n in all_new_nodes if n.id != sym_id]
                            all_new_nodes.append(api_node)

            # Update commit state
            self._graph_store.save_repo_state(repo_name, current_commit)

            report["repos"][repo_name] = {
                "status": "updated",
                "commit": f"{last_commit[:8]} → {current_commit[:8]}",
                "files_added": len(added_files),
                "files_modified": len(modified_files),
                "files_deleted": len(deleted_files),
                "nodes_removed": len([i for i in all_stale_ids if i.startswith(repo_name + ":")]),
                "files_parsed": files_parsed,
                "symbols_parsed": symbols_parsed,
            }

        # Phase 3: Full-index repos that had no prior commit
        for repo_config in repos_needing_full:
            # Remove existing nodes for this repo (if any orphans)
            removed = graph.remove_nodes_by_repo(repo_config.name)
            all_stale_ids.extend(removed)
            # Re-index fully
            await self._index_repo(graph, repo_config)
            try:
                import git as gitmodule
                repo = gitmodule.Repo(str(repo_config.path))
                self._graph_store.save_repo_state(repo_config.name, repo.head.commit.hexsha)
            except Exception:
                pass

        # Phase 4: Re-resolve cross-file refs (on full graph)
        self._resolve_cross_file_refs(graph)

        # Phase 5: Re-detect cross-repo links (on full graph)
        if self._config.cross_repo.detect_api_contracts:
            self._detect_api_contracts(graph)
        if self._config.cross_repo.detect_shared_imports:
            self._detect_shared_imports(graph)

        # Phase 6: Summarisation for new/changed nodes only
        if mode == "heuristic":
            for node in all_new_nodes:
                node.summary = self._heuristic_summary(node, graph)
        elif mode == "llm" and self._llm_client is not None:
            await self._generate_llm_summaries_for_nodes(graph, all_new_nodes)

        # Phase 7: Embed new/changed nodes only
        if all_new_nodes:
            await self._embed_nodes(all_new_nodes, mode, graph)

        # Phase 8: Persist delta
        if all_stale_ids:
            self._graph_store.delete_nodes_by_ids(all_stale_ids)
            self._vector_store.delete(all_stale_ids)

        if all_new_nodes or all_new_edges:
            self._graph_store.save_partial(all_new_nodes, all_new_edges)

        # Also persist nodes from full-indexed repos
        if repos_needing_full:
            full_nodes = []
            full_edges = []
            for repo_config in repos_needing_full:
                for n in graph.nodes_by_repo(repo_config.name):
                    full_nodes.append(n)
                for e in graph.all_edges():
                    src = graph.get_node(e.source)
                    tgt = graph.get_node(e.target)
                    if src and tgt and (src.repo == repo_config.name or tgt.repo == repo_config.name):
                        full_edges.append(e)
            if full_nodes:
                await self._embed_nodes(full_nodes, mode, graph)
                self._graph_store.save_partial(full_nodes, full_edges)

        total_removed = len(all_stale_ids)
        total_added = len(all_new_nodes)
        logger.info(
            "Incremental index complete: -%d stale, +%d new nodes (graph total: %d nodes, %d edges)",
            total_removed, total_added, graph.node_count, graph.edge_count,
        )
        report["total_removed"] = total_removed
        report["total_added"] = total_added
        report["graph_nodes"] = graph.node_count
        report["graph_edges"] = graph.edge_count

        return graph, report

    async def _index_repo(self, graph: CodeGraph, repo_config) -> None:
        """Parse a single repository and add nodes/edges to the graph."""
        repo_name = repo_config.name
        repo_path = Path(repo_config.path)
        language = repo_config.language_hint

        # Create repo-level node
        repo_node = Node(
            id=f"{repo_name}:",
            repo=repo_name,
            kind=NodeKind.REPOSITORY,
            name=repo_name,
            path=str(repo_path),
        )
        graph.add_node(repo_node)

        # Walk files — module nodes are created lazily so directories
        # that contain no parseable code never appear in the graph.
        modules_seen: dict[str, str] = {}  # module path → node id
        files_indexed = 0
        symbols_indexed = 0

        def _ensure_module(rel_dir: str) -> str:
            """Create module node (and ancestors) on demand, return its id."""
            if not rel_dir:
                return f"{repo_name}:"
            if rel_dir in modules_seen:
                return modules_seen[rel_dir]

            # Ensure parent exists first
            parent_dir = os.path.dirname(rel_dir)
            parent_id = _ensure_module(parent_dir)

            module_id = f"{repo_name}:{rel_dir}/"
            module_node = Node(
                id=module_id,
                repo=repo_name,
                kind=NodeKind.MODULE,
                name=os.path.basename(rel_dir),
                path=rel_dir,
            )
            graph.add_node(module_node)
            modules_seen[rel_dir] = module_id
            graph.add_edge(Edge(
                source=parent_id,
                target=module_id,
                kind=EdgeKind.CONTAINS,
            ))
            return module_id

        for root, dirs, files in os.walk(str(repo_path)):
            # Filter excluded directories
            dirs[:] = [
                d for d in dirs
                if not self._is_excluded(d)
            ]

            rel_dir = os.path.relpath(root, str(repo_path))
            if rel_dir == ".":
                rel_dir = ""

            for filename in files:
                if self._is_excluded(filename):
                    continue

                file_path = os.path.join(root, filename)
                rel_path = os.path.relpath(file_path, str(repo_path))

                # Check file size
                try:
                    size_kb = os.path.getsize(file_path) / 1024
                    if size_kb > self._config.indexing.max_file_size_kb:
                        logger.debug("Skipping large file: %s (%.0f KB)", rel_path, size_kb)
                        continue
                except OSError:
                    continue

                # Parse file
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

                # Create file node
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

                # Link file to module/repo (creates module node if needed)
                parent_id = _ensure_module(rel_dir)
                graph.add_edge(Edge(
                    source=parent_id,
                    target=file_id,
                    kind=EdgeKind.CONTAINS,
                ))

                # Add symbol nodes
                for sym in symbols:
                    base_id = f"{repo_name}:{rel_path}::{sym.name}"
                    sym_id = base_id
                    suffix = 2
                    while graph.has_node(sym_id):
                        sym_id = f"{base_id}_{suffix}"
                        suffix += 1

                    node_kind = self._map_symbol_kind(sym.kind)

                    sym_node = Node(
                        id=sym_id,
                        repo=repo_name,
                        kind=node_kind,
                        name=sym.name,
                        path=rel_path,
                        start_line=sym.start_line,
                        end_line=sym.end_line,
                        raw_code=sym.code,
                        signature=sym.signature,
                        docstring=sym.docstring,
                    )

                    # Git blame for temporal data
                    self._populate_git_metadata(sym_node, repo_path)

                    graph.add_node(sym_node)
                    symbols_indexed += 1

                    # CONTAINS edge
                    graph.add_edge(Edge(
                        source=file_id,
                        target=sym_id,
                        kind=EdgeKind.CONTAINS,
                    ))

                    # CALLS edges from parsed call info
                    for callee_name in sym.calls:
                        callee_id = self._resolve_symbol(callee_name, repo_name, rel_path, graph)
                        if callee_id:
                            graph.add_edge(Edge(
                                source=sym_id,
                                target=callee_id,
                                kind=EdgeKind.CALLS,
                            ))

                    # IMPORTS edges
                    for import_name in sym.imports:
                        import_id = self._resolve_symbol(import_name, repo_name, rel_path, graph)
                        if import_id:
                            graph.add_edge(Edge(
                                source=sym_id,
                                target=import_id,
                                kind=EdgeKind.IMPORTS,
                            ))

                    # API endpoint detection
                    for dec in sym.decorators:
                        endpoint_info = self._parse_endpoint_decorator(dec)
                        if endpoint_info:
                            sym_node = graph.get_node(sym_id)
                            if sym_node:
                                # Re-tag as API_ENDPOINT
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
                                    last_modified=sym_node.last_modified,
                                    modification_count=sym_node.modification_count,
                                )
                                graph.add_node(api_node)  # overwrite

        logger.info(
            "Repo %s: %d files, %d symbols",
            repo_name, files_indexed, symbols_indexed,
        )

    def _is_excluded(self, name: str) -> bool:
        """Check if a file/dir name matches any exclusion pattern."""
        return any(
            fnmatch.fnmatch(name, pattern)
            for pattern in self._config.indexing.excluded_patterns
        )

    @staticmethod
    def _map_symbol_kind(kind_str: str) -> NodeKind:
        mapping = {
            "function": NodeKind.FUNCTION,
            "method": NodeKind.METHOD,
            "class": NodeKind.CLASS,
            "type": NodeKind.TYPE,
            "constant": NodeKind.CONSTANT,
        }
        return mapping.get(kind_str, NodeKind.FUNCTION)

    @staticmethod
    def _resolve_symbol(
        name: str,
        repo_name: str,
        current_path: str,
        graph: CodeGraph,
    ) -> Optional[str]:
        """Try to resolve a symbol name to a node ID in the graph."""
        # Exact match within repo
        for node in graph.nodes_by_repo(repo_name):
            if node.name == name and node.is_symbol:
                return node.id
        return None

    @staticmethod
    def _populate_git_metadata(node: Node, repo_path: Path) -> None:
        """Populate git metadata for a node — lightweight, no blame."""
        # Git blame is too slow for large repos. Use git log per-file instead,
        # cached at the class level to avoid repeated calls.
        if not hasattr(IndexingService, '_git_file_cache'):
            IndexingService._git_file_cache = {}

        cache_key = f"{repo_path}:{node.path}"
        if cache_key in IndexingService._git_file_cache:
            cached = IndexingService._git_file_cache[cache_key]
            node.last_modified = cached.get('last_modified')
            node.modification_count = cached.get('modification_count', 0)
            return

        try:
            import git
            repo = git.Repo(str(repo_path))
            if node.path:
                commits = list(repo.iter_commits(paths=str(node.path), max_count=50))
                if commits:
                    info = {
                        'last_modified': commits[0].committed_datetime.isoformat(),
                        'modification_count': len(commits),
                    }
                    IndexingService._git_file_cache[cache_key] = info
                    node.last_modified = info['last_modified']
                    node.modification_count = info['modification_count']
                else:
                    IndexingService._git_file_cache[cache_key] = {}
        except Exception:
            IndexingService._git_file_cache[cache_key] = {}

    @staticmethod
    def _parse_endpoint_decorator(decorator: str) -> Optional[dict]:
        """Parse a decorator to detect API endpoint info."""
        import re
        patterns = [
            r'@\w+\.(get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)',
            r'@app\.route\s*\(\s*["\']([^"\']+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, decorator, re.IGNORECASE)
            if match:
                groups = match.groups()
                if len(groups) == 2:
                    return {"method": groups[0].upper(), "endpoint": groups[1]}
                return {"method": "GET", "endpoint": groups[0]}
        return None

    def _detect_api_contracts(self, graph: CodeGraph) -> None:
        """Detect cross-repo API call relationships."""
        import re

        # Collect all API endpoints
        endpoints: dict[str, str] = {}  # url → node_id
        for node in graph.all_nodes():
            if node.kind == NodeKind.API_ENDPOINT and node.raw_code:
                for dec in (node.docstring or "").split("\n"):
                    pass  # future: parse route from docstring
                # Try to extract from decorators in code
                for match in re.finditer(
                    r'@\w+\.(get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)',
                    node.raw_code, re.IGNORECASE,
                ):
                    url = match.group(2)
                    endpoints[url] = node.id

        # Find HTTP client calls matching endpoints
        url_call_patterns = [
            r'(?:requests|httpx|aiohttp)\.(get|post|put|delete|patch)\s*\([^)]*["\']([^"\']*)',
            r'fetch\s*\(\s*[`"\']([^`"\']+)',
        ]
        for node in graph.symbol_nodes():
            if not node.raw_code:
                continue
            for pattern in url_call_patterns:
                for match in re.finditer(pattern, node.raw_code, re.IGNORECASE):
                    url = match.group(2) if match.lastindex and match.lastindex >= 2 else match.group(1)
                    # Try to match against known endpoints
                    for ep_url, ep_id in endpoints.items():
                        if ep_url in url and graph.get_node(ep_id) is not None:
                            ep_node = graph.get_node(ep_id)
                            if ep_node and ep_node.repo != node.repo:
                                graph.add_edge(Edge(
                                    source=node.id,
                                    target=ep_id,
                                    kind=EdgeKind.API_CALLS,
                                    metadata={"url": url},
                                ))
                                logger.info(
                                    "Cross-repo API call: %s → %s (%s)",
                                    node.id, ep_id, url,
                                )

    def _detect_shared_imports(self, graph: CodeGraph) -> None:
        """Detect shared library usage across repos."""
        # Build import index: symbol_name → list of (repo, node_id) that define it
        definitions: dict[str, list[tuple[str, str]]] = {}
        for node in graph.symbol_nodes():
            definitions.setdefault(node.name, []).append((node.repo, node.id))

        # Find imports that cross repo boundaries
        for edge in list(graph.all_edges()):
            if edge.kind == EdgeKind.IMPORTS:
                src = graph.get_node(edge.source)
                tgt = graph.get_node(edge.target)
                if src and tgt and src.repo != tgt.repo:
                    graph.add_edge(Edge(
                        source=edge.source,
                        target=edge.target,
                        kind=EdgeKind.SHARED_LIB,
                    ))

    # ------------------------------------------------------------------
    # Cross-file symbol resolution
    # ------------------------------------------------------------------

    def _resolve_cross_file_refs(self, graph: CodeGraph) -> int:
        """Scan every symbol's code for references to other known symbols.

        Creates CALLS, USES_TYPE, and INHERITS edges across files.
        Language-agnostic: uses tree-sitter identifier extraction
        (with regex fallback) and matches against the symbol name index.

        Returns the number of new edges created.
        """
        from mimir.domain.lang import detect_language

        # Guard: if the parser doesn't support identifier extraction, use regex fallback
        if not hasattr(self._parser, 'extract_identifiers'):
            logger.warning("Parser does not support extract_identifiers — skipping cross-file resolution")
            return 0

        # 1. Build name → [node] index (only symbols, not containers)
        name_index: dict[str, list[Node]] = {}
        for node in graph.all_nodes():
            if node.is_symbol:
                name_index.setdefault(node.name, []).append(node)

        # Skip very common names that would cause excessive false positives.
        # A name that appears in >20 nodes is likely too generic (e.g. "init").
        ambiguous_names = {
            name for name, nodes in name_index.items()
            if len(nodes) > 20
        }

        # 2. For each symbol, extract identifiers from raw_code and resolve
        edges_created = 0
        seen_edges: set[tuple[str, str, str]] = set()  # (source, target, kind)

        # Collect all existing non-CONTAINS edges to avoid duplicates
        for edge in graph.all_edges():
            if edge.kind != EdgeKind.CONTAINS:
                seen_edges.add((edge.source, edge.target, edge.kind.value))

        for node in graph.all_nodes():
            if not node.is_symbol or not node.raw_code:
                continue

            # Extract identifiers from code
            lang = detect_language(node.path) if node.path else None
            identifiers = self._parser.extract_identifiers(
                node.raw_code, language=lang, file_path=node.path,
            )

            # Also check for inheritance in the signature line
            inherits_names = self._detect_inheritance(node)

            for ident in identifiers:
                if ident == node.name:
                    continue  # skip self-reference
                if ident in ambiguous_names:
                    continue

                targets = name_index.get(ident)
                if not targets:
                    continue

                for target in targets:
                    # Skip self-references
                    if target.id == node.id:
                        continue

                    # Determine edge kind
                    if ident in inherits_names:
                        edge_kind = EdgeKind.INHERITS
                    elif target.kind in (NodeKind.CLASS, NodeKind.TYPE):
                        edge_kind = EdgeKind.USES_TYPE
                    else:
                        edge_kind = EdgeKind.CALLS

                    edge_key = (node.id, target.id, edge_kind.value)
                    if edge_key in seen_edges:
                        continue
                    seen_edges.add(edge_key)

                    graph.add_edge(Edge(
                        source=node.id,
                        target=target.id,
                        kind=edge_kind,
                    ))
                    edges_created += 1

        logger.info("Cross-file resolution: %d new edges created", edges_created)
        return edges_created

    @staticmethod
    def _detect_inheritance(node: Node) -> set[str]:
        """Extract type names from a class/struct/enum signature that
        indicate inheritance, conformance, or implementation.

        Language-agnostic: looks for common patterns in the first line:
          class Foo(Bar, Baz)         — Python
          class Foo : Bar, Baz        — Swift, Kotlin, C#
          class Foo extends Bar       — Java, JS/TS
          class Foo implements Bar    — Java
          struct Foo : Protocol       — Swift
          type Foo struct { embedded } — Go (handled separately)
        """
        import re

        if node.kind not in (NodeKind.CLASS, NodeKind.TYPE):
            return set()

        sig = node.signature or ""
        if not sig:
            # Use the first line of raw_code
            if node.raw_code:
                sig = node.raw_code.split("\n", 1)[0]
            else:
                return set()

        names: set[str] = set()

        # Pattern 1: parenthesised bases — class Foo(Bar, Baz):
        m = re.search(r'\(\s*([^)]+)\)', sig)
        if m:
            for part in m.group(1).split(","):
                # Strip generics, default args, etc.
                base = re.split(r'[<\[\(=]', part.strip())[0].strip()
                if base and re.match(r'^[A-Z]\w*$', base):
                    names.add(base)

        # Pattern 2: colon-separated — class Foo : Bar, Baz
        m = re.search(r'(?:class|struct|enum|protocol|interface)\s+\w+\s*:\s*(.+?)(?:\{|where|$)', sig)
        if m:
            for part in m.group(1).split(","):
                base = re.split(r'[<\[\(]', part.strip())[0].strip()
                if base and re.match(r'^[A-Z]\w*$', base):
                    names.add(base)

        # Pattern 3: extends / implements keywords
        for kw in ("extends", "implements"):
            m = re.search(rf'{kw}\s+([\w,\s<>]+?)(?:\{{|implements|$)', sig)
            if m:
                for part in m.group(1).split(","):
                    base = re.split(r'[<\[\(]', part.strip())[0].strip()
                    if base and re.match(r'^[A-Z]\w*$', base):
                        names.add(base)

        return names

    # ------------------------------------------------------------------
    # Summarisation
    # ------------------------------------------------------------------

    def _generate_heuristic_summaries(self, graph: CodeGraph) -> None:
        """Generate summaries from signatures + docstrings + callee names."""
        for node in graph.all_nodes():
            node.summary = self._heuristic_summary(node, graph)

    @staticmethod
    def _heuristic_summary(node: Node, graph: CodeGraph) -> str:
        """Build a structured summary without LLM."""
        parts: list[str] = []

        if node.kind in (NodeKind.FUNCTION, NodeKind.METHOD):
            if node.signature:
                parts.append(node.signature)
            if node.docstring:
                parts.append(node.docstring[:200])
            callees = graph.get_callees(node.id)
            if callees:
                parts.append(f"Calls: {', '.join(c.name for c in callees[:10])}")
            callers = graph.get_callers(node.id)
            if callers:
                parts.append(f"Called by: {', '.join(c.name for c in callers[:10])}")
        elif node.kind == NodeKind.FILE:
            children = graph.get_children(node.id)
            parts.append(f"File: {node.path}")
            for child in children[:20]:
                sig = child.signature or child.name
                doc = f" — {child.docstring[:80]}" if child.docstring else ""
                parts.append(f"  {sig}{doc}")
        elif node.kind == NodeKind.MODULE:
            children = graph.get_children(node.id)
            parts.append(f"Module: {node.name}")
            for child in children:
                symbol_count = len(graph.get_children(child.id))
                parts.append(f"  {child.name} ({symbol_count} symbols)")
        elif node.kind == NodeKind.REPOSITORY:
            modules = graph.get_children(node.id)
            parts.append(f"Repository: {node.name}")
            for mod in modules:
                file_count = len(graph.get_children(mod.id))
                parts.append(f"  {mod.name}/ ({file_count} files)")

        return "\n".join(parts) if parts else node.name

    async def _generate_llm_summaries(self, graph: CodeGraph) -> None:
        """Full LLM summarization (llm mode)."""
        assert self._llm_client is not None
        import asyncio
        from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

        # Bottom-up: summarize symbols first, then files, modules, repos
        levels = [
            ("Symbols", [n for n in graph.all_nodes() if n.is_symbol and n.raw_code]),
            ("Files", list(graph.nodes_by_kind(NodeKind.FILE))),
            ("Modules", list(graph.nodes_by_kind(NodeKind.MODULE))),
            ("Repos", list(graph.nodes_by_kind(NodeKind.REPOSITORY))),
        ]

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("{task.completed}/{task.total}"),
            transient=True,
        ) as progress:
            for level_name, level_nodes in levels:
                if not level_nodes:
                    continue

                prompts: list[str] = []
                target_nodes: list[Node] = []

                for node in level_nodes:
                    prompt = self._build_summary_prompt(node, graph)
                    prompts.append(prompt)
                    target_nodes.append(node)

                task_id = progress.add_task(f"[cyan]Summarizing {level_name}...", total=len(prompts))

                async def _summarize(node, prompt):
                    try:
                        res = await self._llm_client.complete(prompt)
                        if res:
                            node.summary = res
                    except Exception as exc:
                        logger.error("LLM call failed: %s", exc)
                    progress.update(task_id, advance=1)

                tasks = [_summarize(n, p) for n, p in zip(target_nodes, prompts)]
                await asyncio.gather(*tasks)

    @staticmethod
    def _build_summary_prompt(node: Node, graph: CodeGraph) -> str:
        """Build a prompt for LLM summarization."""
        lang = detect_language(node.path) or "unknown"
        location = f"{node.repo}/{node.path}" if node.path else node.repo

        parts = [
            f"Summarize this {node.kind.value} from `{location}` ({lang}) in 2-3 sentences. "
            "Be specific about what the code does based on its actual content. "
            "Do not speculate about frameworks or technologies not evident in the code.",
            "",
        ]
        if node.raw_code:
            parts.append(f"```{lang}\n{node.raw_code[:2000]}\n```")
        elif node.is_container:
            children = graph.get_children(node.id)
            children_info = ", ".join(c.name for c in children[:20])
            parts.append(f"Contains: {children_info}")

        # Add dependency context
        callees = graph.get_callees(node.id)
        if callees:
            callee_info = ", ".join(
                f"{c.name} ({c.summary[:50] if c.summary else 'no summary'})"
                for c in callees[:5]
            )
            parts.append(f"\nCalls: {callee_info}")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    @staticmethod
    def _embedding_text(node: Node, graph: CodeGraph) -> str:
        """Build grounded text for embedding a node.

        Symbols use their raw code.  Containers use a concatenation of
        their path, children's signatures/names, and (if available) a
        short LLM summary — so the embedding is always anchored in real
        identifiers and never dominated by a potentially hallucinated
        summary.
        """
        if node.is_symbol:
            return node.raw_code or node.signature or node.name

        # Container: build from grounded content
        parts: list[str] = []

        # Path carries semantic signal (e.g. "Features/Home/HomeView.swift")
        if node.path:
            parts.append(node.path)

        # Children's signatures / names
        children = graph.get_children(node.id)
        for child in children[:30]:
            sig = child.signature or child.name
            parts.append(sig)

        # Append summary as minority signal (not dominant)
        if node.summary and len(parts) > 0:
            parts.append(node.summary[:500])

        return "\n".join(parts) if parts else node.name

    async def _embed_graph(self, graph: CodeGraph, mode: str) -> None:
        """Embed all nodes and upsert into vector store."""
        from mimir.domain.models import SYMBOL_KINDS
        from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

        texts: list[str] = []
        nodes: list[Node] = []

        for node in graph.all_nodes():
            if mode == "none" and not node.is_symbol:
                continue  # none mode: only embed leaf symbols

            text = self._embedding_text(node, graph)
            if text:
                texts.append(text[:4000])  # cap text length
                nodes.append(node)

        if not texts:
            logger.warning("No texts to embed")
            return

        logger.info("Embedding %d nodes...", len(texts))

        # Batch embed
        batch_size = self._config.embeddings.batch_size
        all_embeddings: list[list[float]] = []
        total_batches = (len(texts) + batch_size - 1) // batch_size

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("{task.completed}/{task.total} batches"),
            transient=True,
        ) as progress:
            task_id = progress.add_task("[green]Embedding nodes...", total=total_batches)
            
            for i in range(0, len(texts), batch_size):
                batch = texts[i : i + batch_size]
                embeddings = await self._embedder.embed_batch(batch)
                all_embeddings.extend(embeddings)
                progress.update(task_id, advance=1)

        # Assign to nodes and upsert to vector store
        ids: list[str] = []
        metadatas: list[dict] = []
        for node, embedding in zip(nodes, all_embeddings):
            node.embedding = embedding
            ids.append(node.id)
            metadatas.append({
                "repo": node.repo,
                "kind": node.kind.value,
                "path": node.path or "",
                "last_modified": node.last_modified or "",
            })

        self._vector_store.upsert(
            ids=ids,
            embeddings=all_embeddings,
            metadatas=metadatas,
            documents=texts,
        )

        logger.info("Embedded and stored %d vectors", len(ids))

    async def _embed_nodes(self, nodes_to_embed: list[Node], mode: str, graph: Optional[CodeGraph] = None) -> None:
        """Embed a specific list of nodes and upsert into vector store.

        Unlike ``_embed_graph`` which iterates the entire graph, this only
        processes the provided nodes — used by incremental indexing.
        """
        texts: list[str] = []
        nodes: list[Node] = []

        for node in nodes_to_embed:
            if mode == "none" and not node.is_symbol:
                continue
            text = self._embedding_text(node, graph) if graph else (node.raw_code or node.summary or node.name)
            if text:
                texts.append(text[:4000])
                nodes.append(node)

        if not texts:
            return

        logger.info("Embedding %d changed nodes...", len(texts))

        batch_size = self._config.embeddings.batch_size
        all_embeddings: list[list[float]] = []

        for i in range(0, len(texts), batch_size):
            batch_texts = texts[i : i + batch_size]
            embeddings = await self._embedder.embed_batch(batch_texts)
            all_embeddings.extend(embeddings)

        ids: list[str] = []
        metadatas: list[dict] = []
        for node, embedding in zip(nodes, all_embeddings):
            node.embedding = embedding
            ids.append(node.id)
            metadatas.append({
                "repo": node.repo,
                "kind": node.kind.value,
                "path": node.path or "",
                "last_modified": node.last_modified or "",
            })

        self._vector_store.upsert(
            ids=ids,
            embeddings=all_embeddings,
            metadatas=metadatas,
            documents=texts,
        )
        logger.info("Embedded and stored %d changed vectors", len(ids))

    async def _generate_llm_summaries_for_nodes(
        self, graph: CodeGraph, nodes_to_summarise: list[Node],
    ) -> None:
        """LLM-summarise only the given nodes (incremental mode)."""
        assert self._llm_client is not None
        import asyncio

        prompts = [self._build_summary_prompt(n, graph) for n in nodes_to_summarise]

        async def _summarize(node, prompt):
            try:
                res = await self._llm_client.complete(prompt)
                if res:
                    node.summary = res
            except Exception as exc:
                logger.error("LLM call failed for %s: %s", node.id, exc)

        tasks = [_summarize(n, p) for n, p in zip(nodes_to_summarise, prompts)]
        await asyncio.gather(*tasks)
        logger.info("LLM-summarised %d nodes", len(nodes_to_summarise))
