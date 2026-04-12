"""Indexing service — orchestrates parsing, graph building, summarization, and embedding.

This is the primary application service for Milestone 1+2.
It receives all infrastructure dependencies via constructor injection.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from mimir.domain.config import MimirConfig
from mimir.domain.errors import ParsingError
from mimir.domain.graph import CodeGraph
from mimir.domain.models import Edge, EdgeKind, Node, NodeKind
from mimir.ports.embedder import Embedder
from mimir.ports.graph_store import GraphStore
from mimir.domain.lang import detect_language
from mimir.ports.parser import Parser, Symbol
from mimir.ports.vector_store import VectorStore
from mimir.services.graph_linker import (
    detect_api_contracts,
    detect_inheritance,
    detect_shared_imports,
    normalize_route,
    resolve_cross_file_refs,
)
from mimir.services.summarizer import generate_heuristic_summaries, heuristic_summary

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
    ) -> None:
        self._config = config
        self._parser = parser
        self._embedder = embedder
        self._vector_store = vector_store
        self._graph_store = graph_store

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
        resolve_cross_file_refs(graph, self._parser)

        # Phase 3: Cross-repo link detection
        if self._config.cross_repo.detect_api_contracts:
            detect_api_contracts(graph)
        if self._config.cross_repo.detect_shared_imports:
            detect_shared_imports(graph)

        logger.info("Graph built: %s", graph.stats())

        # Phase 4: Summarization
        if mode == "heuristic":
            generate_heuristic_summaries(graph)

        # Phase 5: Embedding
        await self._embed_and_upsert(graph, mode, show_progress=True)

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
                    node, edges = self._add_symbol_to_graph(
                        graph, sym, repo_name, rel_path, file_id, repo_path,
                    )
                    all_new_nodes.append(node)
                    all_new_edges.extend(edges)
                    symbols_parsed += 1

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
        resolve_cross_file_refs(graph, self._parser)

        # Phase 5: Re-detect cross-repo links (on full graph)
        if self._config.cross_repo.detect_api_contracts:
            detect_api_contracts(graph)
        if self._config.cross_repo.detect_shared_imports:
            detect_shared_imports(graph)

        # Phase 6: Summarisation for new/changed nodes only
        if mode == "heuristic":
            for node in all_new_nodes:
                node.summary = heuristic_summary(node, graph)

        # Phase 7: Embed new/changed nodes only
        if all_new_nodes:
            await self._embed_and_upsert(graph, mode, nodes_to_embed=all_new_nodes)

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
                await self._embed_and_upsert(graph, mode, nodes_to_embed=full_nodes)
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
                    self._add_symbol_to_graph(
                        graph, sym, repo_name, rel_path, file_id, repo_path,
                    )
                    symbols_indexed += 1

        logger.info(
            "Repo %s: %d files, %d symbols",
            repo_name, files_indexed, symbols_indexed,
        )

    async def index_files(
        self,
        graph: CodeGraph,
        repo_name: str,
        repo_path: Path,
        changed_files: set[str],
        deleted_files: set[str],
        language_hint: Optional[str] = None,
    ) -> tuple[list[str], list[Node], list[Edge]]:
        """Re-index specific files within a loaded graph (used by file watcher).

        This is a lightweight alternative to ``index_incremental`` that
        operates on individual files without git diff.  It never calls
        the LLM — only heuristic summaries are used.

        Returns ``(removed_ids, new_nodes, new_edges)``.
        """
        removed_ids: list[str] = []
        new_nodes: list[Node] = []
        new_edges: list[Edge] = []

        # 1. Remove stale nodes for deleted + changed files
        files_to_remove = deleted_files | changed_files
        if files_to_remove:
            removed_ids = graph.remove_nodes_by_paths(repo_name, files_to_remove)

        # 2. Re-parse changed files
        for rel_path in sorted(changed_files):
            file_path = str(repo_path / rel_path)

            if self._is_excluded(os.path.basename(rel_path)):
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
            new_nodes.append(file_node)

            # Link file to parent module/repo
            rel_dir = os.path.dirname(rel_path)
            parent_id = f"{repo_name}:{rel_dir}/" if rel_dir else f"{repo_name}:"
            if graph.has_node(parent_id):
                edge = Edge(source=parent_id, target=file_id, kind=EdgeKind.CONTAINS)
                graph.add_edge(edge)
                new_edges.append(edge)

            # Add symbol nodes (lightweight — no CALLS/IMPORTS/API resolution)
            for sym in symbols:
                node, edges = self._add_symbol_to_graph(
                    graph, sym, repo_name, rel_path, file_id, repo_path,
                    resolve_edges=False,
                )
                new_nodes.append(node)
                new_edges.extend(edges)

        # 3. Affected-set cross-file resolution (only for new symbols)
        new_symbol_nodes = [n for n in new_nodes if n.is_symbol]
        if new_symbol_nodes and hasattr(self._parser, 'extract_identifiers'):
            xref_edges = self._resolve_affected_refs(graph, new_symbol_nodes)
            new_edges.extend(xref_edges)

        # 4. Heuristic summaries (never LLM)
        for node in new_nodes:
            node.summary = heuristic_summary(node, graph)

        # 5. Embed new nodes
        if new_nodes:
            await self._embed_and_upsert(graph, "heuristic", nodes_to_embed=new_nodes)

        logger.info(
            "index_files: -%d removed, +%d nodes, +%d edges",
            len(removed_ids), len(new_nodes), len(new_edges),
        )
        return removed_ids, new_nodes, new_edges

    def _resolve_affected_refs(
        self, graph: CodeGraph, new_symbols: list[Node],
    ) -> list[Edge]:
        """Cross-file resolution for only the affected symbols.

        Scans new symbols' code for references to existing symbols,
        and scans existing symbols' code for references to new symbol names.
        Much faster than full ``_resolve_cross_file_refs`` for single-file changes.
        """
        from mimir.domain.lang import detect_language

        # Build full name → [node] index
        name_index: dict[str, list[Node]] = {}
        for node in graph.all_nodes():
            if node.is_symbol:
                name_index.setdefault(node.name, []).append(node)

        ambiguous_names = {
            name for name, nodes in name_index.items()
            if len(nodes) > 20
        }

        new_edges: list[Edge] = []
        seen_edges: set[tuple[str, str, str]] = set()

        # Collect existing non-CONTAINS edges to avoid duplicates
        for edge in graph.all_edges():
            if edge.kind != EdgeKind.CONTAINS:
                seen_edges.add((edge.source, edge.target, edge.kind.value))

        new_symbol_ids = {n.id for n in new_symbols}
        new_symbol_names = {n.name for n in new_symbols}

        def _try_add_edge(source_id: str, target: Node, ident: str, inherits_names: set[str]) -> None:
            if target.id == source_id:
                return
            if ident in inherits_names:
                edge_kind = EdgeKind.INHERITS
            elif target.kind in (NodeKind.CLASS, NodeKind.TYPE):
                edge_kind = EdgeKind.USES_TYPE
            else:
                edge_kind = EdgeKind.CALLS

            edge_key = (source_id, target.id, edge_kind.value)
            if edge_key in seen_edges:
                return
            seen_edges.add(edge_key)

            edge = Edge(source=source_id, target=target.id, kind=edge_kind)
            graph.add_edge(edge)
            new_edges.append(edge)

        # A. Scan new symbols → find what they reference
        for node in new_symbols:
            if not node.raw_code:
                continue
            lang = detect_language(node.path) if node.path else None
            identifiers = self._parser.extract_identifiers(
                node.raw_code, language=lang, file_path=node.path,
            )
            inherits_names = detect_inheritance(node)

            for ident in identifiers:
                if ident == node.name or ident in ambiguous_names:
                    continue
                targets = name_index.get(ident)
                if not targets:
                    continue
                for target in targets:
                    _try_add_edge(node.id, target, ident, inherits_names)

        # B. Scan existing symbols → find references TO new symbol names
        for node in graph.all_nodes():
            if not node.is_symbol or not node.raw_code:
                continue
            if node.id in new_symbol_ids:
                continue  # already handled above

            # Quick pre-filter: does raw_code contain any new symbol name?
            if not any(name in node.raw_code for name in new_symbol_names if name not in ambiguous_names):
                continue

            lang = detect_language(node.path) if node.path else None
            identifiers = self._parser.extract_identifiers(
                node.raw_code, language=lang, file_path=node.path,
            )
            inherits_names = detect_inheritance(node)

            for ident in identifiers:
                if ident not in new_symbol_names or ident in ambiguous_names:
                    continue
                targets = name_index.get(ident)
                if not targets:
                    continue
                for target in targets:
                    if target.id not in new_symbol_ids:
                        continue  # only add edges TO new symbols
                    _try_add_edge(node.id, target, ident, inherits_names)

        logger.info("Affected cross-file resolution: %d new edges", len(new_edges))
        return new_edges

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

    def _add_symbol_to_graph(
        self,
        graph: CodeGraph,
        sym: "Symbol",
        repo_name: str,
        rel_path: str,
        file_id: str,
        repo_path: Path,
        *,
        resolve_edges: bool = True,
    ) -> tuple[Node, list[Edge]]:
        """Create a symbol node, add it to *graph*, and return ``(node, edges)``.

        Handles ID deduplication, git metadata, CONTAINS edge, and optionally
        CALLS/IMPORTS/API-endpoint detection.  Set *resolve_edges* to ``False``
        for lightweight paths (file watcher) that skip cross-symbol linking.
        """
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

        edges: list[Edge] = []

        # CONTAINS edge
        contains = Edge(source=file_id, target=sym_id, kind=EdgeKind.CONTAINS)
        graph.add_edge(contains)
        edges.append(contains)

        if resolve_edges:
            # CALLS edges
            for callee_name in sym.calls:
                callee_id = self._resolve_symbol(callee_name, repo_name, rel_path, graph)
                if callee_id:
                    e = Edge(source=sym_id, target=callee_id, kind=EdgeKind.CALLS)
                    graph.add_edge(e)
                    edges.append(e)

            # IMPORTS edges
            for import_name in sym.imports:
                import_id = self._resolve_symbol(import_name, repo_name, rel_path, graph)
                if import_id:
                    e = Edge(source=sym_id, target=import_id, kind=EdgeKind.IMPORTS)
                    graph.add_edge(e)
                    edges.append(e)

            # API endpoint detection
            for dec in sym.decorators:
                endpoint_info = self._parse_endpoint_decorator(dec)
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
                        graph.add_node(api_node)  # overwrite
                        sym_node = api_node

        return sym_node, edges

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

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    @staticmethod
    def _embedding_text(node: Node, graph: CodeGraph) -> str:
        """Build grounded text for embedding a node.

        Symbols use their raw code augmented with structural context
        (docstring, callers, callees, type relationships).
        Containers use path + children signatures + heuristic summary.
        """
        if node.is_symbol:
            code = node.raw_code or node.signature or node.name

            # Augment with structural metadata
            context_parts: list[str] = []
            if node.path:
                context_parts.append(f"File: {node.path}")
            if node.http_method and node.route_path:
                context_parts.append(f"Route: {node.http_method} {node.route_path}")
            if node.docstring:
                context_parts.append(f"Doc: {node.docstring[:200]}")

            callees = graph.get_callees(node.id)
            if callees:
                names = [c.name for c in callees[:10]]
                context_parts.append(f"Calls: {', '.join(names)}")

            callers = graph.get_callers(node.id)
            if callers:
                names = [c.name for c in callers[:10]]
                context_parts.append(f"Called by: {', '.join(names)}")

            for edge_kind, label in [
                (EdgeKind.INHERITS, "Inherits"),
                (EdgeKind.IMPLEMENTS, "Implements"),
            ]:
                edges = graph.get_outgoing_edges(node.id, edge_kind)
                if edges:
                    targets = []
                    for e in edges[:5]:
                        t = graph.get_node(e.target)
                        if t:
                            targets.append(t.name)
                    if targets:
                        context_parts.append(f"{label}: {', '.join(targets)}")

            if context_parts:
                return code + "\n\n# Context\n" + "\n".join(context_parts)
            return code

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

    async def _embed_texts_batched(
        self,
        texts: list[str],
        on_batch_done: Optional[Callable[[], None]] = None,
    ) -> list[list[float]]:
        """Embed ``texts`` in batches with bounded concurrency.

        Batches are dispatched via ``asyncio.gather`` and capped by a
        semaphore of size ``embeddings.max_concurrent_batches``.  Input order
        is preserved in the returned list (``gather`` returns results in
        submission order).

        ``on_batch_done`` is called once per completed batch and can be used
        by callers (e.g. ``_embed_and_upsert``) to drive a progress bar.
        """
        batch_size = self._config.embeddings.batch_size
        max_concurrent = max(1, self._config.embeddings.max_concurrent_batches)
        sem = asyncio.Semaphore(max_concurrent)

        async def run_batch(chunk: list[str]) -> list[list[float]]:
            async with sem:
                result = await self._embedder.embed_batch(chunk)
            if on_batch_done is not None:
                on_batch_done()
            return result

        chunks = [texts[i : i + batch_size] for i in range(0, len(texts), batch_size)]
        results = await asyncio.gather(*(run_batch(chunk) for chunk in chunks))

        flat: list[list[float]] = []
        for r in results:
            flat.extend(r)
        return flat

    async def _embed_and_upsert(
        self,
        graph: CodeGraph,
        mode: str,
        *,
        nodes_to_embed: Optional[list[Node]] = None,
        show_progress: bool = False,
    ) -> None:
        """Embed nodes and upsert into the vector store.

        When *nodes_to_embed* is ``None`` (full-graph mode) every node in
        *graph* is processed.  Otherwise only the given list is embedded.
        *show_progress* enables a Rich progress bar (used by the full-index
        path).
        """
        source = nodes_to_embed if nodes_to_embed is not None else list(graph.all_nodes())

        texts: list[str] = []
        nodes: list[Node] = []

        for node in source:
            if mode == "none" and not node.is_symbol:
                continue
            text = self._embedding_text(node, graph) if graph else (node.raw_code or node.summary or node.name)
            if text:
                texts.append(text[:4000])
                nodes.append(node)

        if not texts:
            if nodes_to_embed is None:
                logger.warning("No texts to embed")
            return

        logger.info("Embedding %d nodes...", len(texts))

        if show_progress:
            from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

            batch_size = self._config.embeddings.batch_size
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
                all_embeddings = await self._embed_texts_batched(
                    texts,
                    on_batch_done=lambda: progress.update(task_id, advance=1),
                )
        else:
            all_embeddings = await self._embed_texts_batched(texts)

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

