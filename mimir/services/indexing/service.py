"""Indexing service — orchestrates parsing, graph building, summarization, and embedding.

This is the primary application service for Milestone 1+2.
It receives all infrastructure dependencies via constructor injection.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from mimir.domain.config import MimirConfig, SummaryMode
from mimir.domain.graph import CodeGraph
from mimir.domain.models import Edge, EdgeKind, Node, NodeKind
from mimir.ports.embedder import Embedder
from mimir.ports.graph_store import GraphStore
from mimir.ports.parser import Parser
from mimir.ports.vector_store import VectorStore
from mimir.services.graph_linker import (
    detect_api_contracts,
    detect_shared_imports,
    resolve_cross_file_refs,
)
from mimir.services.indexing.builder import IndexingGraphBuilder
from mimir.services.indexing.embedding_pipeline import IndexingEmbeddingPipeline
from mimir.services.summarizer import generate_heuristic_summaries

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
        self._graph_builder = IndexingGraphBuilder(config, parser)
        self._embedding_pipeline = IndexingEmbeddingPipeline(config, embedder, vector_store)

    async def index_all(self, *, mode_override: Optional[SummaryMode | str] = None) -> CodeGraph:
        """Run the full indexing pipeline.

        1. Parse all repos → build CodeGraph
        2. Cross-file symbol resolution (CALLS, USES_TYPE, INHERITS edges)
        3. Cross-repo link detection (API contracts, shared imports)
        4. Generate summaries (based on mode)
        5. Embed nodes
        6. Store in vector DB and SQLite
        """
        mode = SummaryMode(mode_override) if mode_override else self._config.indexing.summary_mode
        logger.info("Starting full index — mode=%s, repos=%d", mode.value, len(self._config.repos))

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
        if mode is SummaryMode.HEURISTIC:
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

    async def index_incremental(self, *, mode_override: Optional[SummaryMode | str] = None) -> tuple[CodeGraph, dict]:
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
        mode = SummaryMode(mode_override) if mode_override else self._config.indexing.summary_mode
        logger.info("Starting incremental index — mode=%s, repos=%d", mode.value, len(self._config.repos))

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
                parent_id, module_nodes, module_edges = self._ensure_module_hierarchy(
                    graph,
                    repo_name,
                    rel_dir,
                    repo_root_path=repo_path,
                )
                all_new_nodes.extend(module_nodes)
                all_new_edges.extend(module_edges)
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
        if mode is SummaryMode.HEURISTIC:
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

    async def refresh_repo(
        self,
        graph: CodeGraph,
        repo_name: str,
        *,
        mode_override: Optional[SummaryMode | str] = None,
    ) -> dict:
        """Re-index one configured repo into the unified graph.

        This path is intended for centrally mirrored repos updated by webhook
        or other admin sync flows. It keeps the unified multi-repo graph model
        intact by removing the repo, rebuilding it from disk, then rerunning
        the global linkers over the assembled graph.
        """
        mode = SummaryMode(mode_override) if mode_override else self._config.indexing.summary_mode
        repo_config = next((r for r in self._config.repos if r.name == repo_name), None)
        if repo_config is None:
            raise ValueError(f"Unknown repo: {repo_name}")

        external_edges_before = self._edge_keys_excluding_repo(graph, repo_name)
        removed_ids = graph.remove_nodes_by_repo(repo_name)
        if removed_ids:
            self._vector_store.delete(removed_ids)

        await self._index_repo(graph, repo_config)

        resolve_cross_file_refs(graph, self._parser)
        if self._config.cross_repo.detect_api_contracts:
            detect_api_contracts(graph)
        if self._config.cross_repo.detect_shared_imports:
            detect_shared_imports(graph)

        if mode is SummaryMode.HEURISTIC:
            generate_heuristic_summaries(graph)

        repo_nodes = list(graph.nodes_by_repo(repo_name))
        if repo_nodes:
            await self._embed_and_upsert(graph, mode, nodes_to_embed=repo_nodes)

        external_edges_after = self._edge_keys_excluding_repo(graph, repo_name)
        full_save_required = external_edges_before != external_edges_after

        if full_save_required:
            self._graph_store.save(graph)
        else:
            if removed_ids:
                self._graph_store.delete_nodes_by_ids(removed_ids)
            repo_edges = self._edges_touching_repo(graph, repo_name)
            if repo_nodes or repo_edges:
                self._graph_store.save_partial(repo_nodes, repo_edges)

        try:
            import git

            repo = git.Repo(str(repo_config.path))
            commit_hash = repo.head.commit.hexsha
            self._graph_store.save_repo_state(repo_name, commit_hash)
        except Exception:
            logger.debug("Could not read git commit for %s", repo_name)
            commit_hash = None

        return {
            "repo": repo_name,
            "removed_nodes": len(removed_ids),
            "repo_nodes": len(repo_nodes),
            "graph_nodes": graph.node_count,
            "graph_edges": graph.edge_count,
            "commit": commit_hash,
            "persist_mode": "full" if full_save_required else "partial",
        }

    @staticmethod
    def _edge_keys_excluding_repo(graph: CodeGraph, repo_name: str) -> set[tuple[str, str, str]]:
        """Return edge identity keys where both endpoints are outside *repo_name*."""
        keys: set[tuple[str, str, str]] = set()
        for edge in graph.all_edges():
            src = graph.get_node(edge.source)
            tgt = graph.get_node(edge.target)
            if src is None or tgt is None:
                continue
            if src.repo == repo_name or tgt.repo == repo_name:
                continue
            keys.add((edge.source, edge.target, edge.kind.value))
        return keys

    @staticmethod
    def _edges_touching_repo(graph: CodeGraph, repo_name: str) -> list[Edge]:
        """Collect edges where either endpoint belongs to *repo_name*."""
        edges: list[Edge] = []
        for edge in graph.all_edges():
            src = graph.get_node(edge.source)
            tgt = graph.get_node(edge.target)
            if src is None or tgt is None:
                continue
            if src.repo == repo_name or tgt.repo == repo_name:
                edges.append(edge)
        return edges

    def _graph_builder_component(self) -> IndexingGraphBuilder:
        builder = getattr(self, "_graph_builder", None)
        if builder is None:
            builder = IndexingGraphBuilder(self._config, self._parser)
            self._graph_builder = builder
        return builder

    def _embedding_pipeline_component(self) -> IndexingEmbeddingPipeline:
        pipeline = getattr(self, "_embedding_pipeline", None)
        if pipeline is None:
            pipeline = IndexingEmbeddingPipeline(
                self._config,
                self._embedder,
                getattr(self, "_vector_store", None),
            )
            self._embedding_pipeline = pipeline
        return pipeline

    async def _index_repo(self, graph: CodeGraph, repo_config) -> None:
        await self._graph_builder_component().index_repo(graph, repo_config)

    async def index_files(
        self,
        graph: CodeGraph,
        repo_name: str,
        repo_path: Path,
        changed_files: set[str],
        deleted_files: set[str],
        language_hint: Optional[str] = None,
    ) -> tuple[list[str], list[Node], list[Edge]]:
        removed_ids, new_nodes, new_edges = await self._graph_builder_component().index_files(
            graph,
            repo_name,
            repo_path,
            changed_files,
            deleted_files,
            language_hint=language_hint,
        )
        if new_nodes:
            await self._embed_and_upsert(graph, "heuristic", nodes_to_embed=new_nodes)
        return removed_ids, new_nodes, new_edges

    def _is_excluded(self, name: str) -> bool:
        return self._graph_builder_component().is_excluded(name)

    @staticmethod
    def _map_symbol_kind(kind_str: str) -> NodeKind:
        return IndexingGraphBuilder.map_symbol_kind(kind_str)

    def _ensure_module_hierarchy(
        self,
        graph: CodeGraph,
        repo_name: str,
        rel_dir: str,
        *,
        repo_root_path: Optional[Path] = None,
    ) -> tuple[str, list[Node], list[Edge]]:
        return self._graph_builder_component().ensure_module_hierarchy(
            graph,
            repo_name,
            rel_dir,
            repo_root_path=repo_root_path,
        )

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
        return self._graph_builder_component().add_symbol_to_graph(
            graph,
            sym,
            repo_name,
            rel_path,
            file_id,
            repo_path,
            resolve_edges=resolve_edges,
        )

    @staticmethod
    def _resolve_symbol(
        name: str,
        repo_name: str,
        current_path: str,
        graph: CodeGraph,
    ) -> Optional[str]:
        del current_path
        return IndexingGraphBuilder.resolve_symbol(name, repo_name, graph)

    @staticmethod
    def _populate_git_metadata(node: Node, repo_path: Path) -> None:
        IndexingGraphBuilder.populate_git_metadata(node, repo_path)

    @staticmethod
    def _parse_endpoint_decorator(decorator: str) -> Optional[dict]:
        return IndexingGraphBuilder.parse_endpoint_decorator(decorator)

    @staticmethod
    def _embedding_text(node: Node, graph: CodeGraph) -> str:
        return IndexingEmbeddingPipeline.embedding_text(node, graph)

    async def _embed_texts_batched(
        self,
        texts: list[str],
        on_batch_done: Optional[Callable[[], None]] = None,
    ) -> list[list[float]]:
        return await self._embedding_pipeline_component().embed_texts_batched(
            texts,
            on_batch_done=on_batch_done,
        )

    async def _embed_and_upsert(
        self,
        graph: CodeGraph,
        mode: str,
        *,
        nodes_to_embed: Optional[list[Node]] = None,
        show_progress: bool = False,
    ) -> None:
        await self._embedding_pipeline_component().embed_and_upsert(
            graph,
            mode,
            nodes_to_embed=nodes_to_embed,
            show_progress=show_progress,
        )
