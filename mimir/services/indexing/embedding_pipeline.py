"""Embedding helpers for the indexing pipeline."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from mimir.domain.config import MimirConfig
from mimir.domain.graph import CodeGraph
from mimir.domain.models import EdgeKind, Node
from mimir.ports.embedder import Embedder
from mimir.ports.vector_store import VectorStore

logger = logging.getLogger(__name__)


class IndexingEmbeddingPipeline:
    """Owns embedding text generation, batching, and vector upserts."""

    def __init__(
        self,
        config: MimirConfig,
        embedder: Embedder,
        vector_store: VectorStore | None,
    ) -> None:
        self._config = config
        self._embedder = embedder
        self._vector_store = vector_store

    @staticmethod
    def embedding_text(node: Node, graph: CodeGraph) -> str:
        if node.is_symbol:
            code = node.raw_code or node.signature or node.name

            context_parts: list[str] = []
            if node.path:
                context_parts.append(f"File: {node.path}")
            if node.http_method and node.route_path:
                context_parts.append(f"Route: {node.http_method} {node.route_path}")
            if node.docstring:
                context_parts.append(f"Doc: {node.docstring[:200]}")

            callees = graph.get_callees(node.id)
            if callees:
                context_parts.append(f"Calls: {', '.join(callee.name for callee in callees[:10])}")

            callers = graph.get_callers(node.id)
            if callers:
                context_parts.append(f"Called by: {', '.join(caller.name for caller in callers[:10])}")

            for edge_kind, label in (
                (EdgeKind.INHERITS, "Inherits"),
                (EdgeKind.IMPLEMENTS, "Implements"),
            ):
                edges = graph.get_outgoing_edges(node.id, edge_kind)
                targets = [
                    target.name
                    for edge in edges[:5]
                    if (target := graph.get_node(edge.target)) is not None
                ]
                if targets:
                    context_parts.append(f"{label}: {', '.join(targets)}")

            if context_parts:
                return code + "\n\n# Context\n" + "\n".join(context_parts)
            return code

        parts: list[str] = []
        if node.path:
            parts.append(node.path)

        for child in graph.get_children(node.id)[:30]:
            parts.append(child.signature or child.name)

        if node.summary and parts:
            parts.append(node.summary[:500])

        return "\n".join(parts) if parts else node.name

    async def embed_texts_batched(
        self,
        texts: list[str],
        on_batch_done: Optional[Callable[[], None]] = None,
    ) -> list[list[float]]:
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
        return [embedding for batch in results for embedding in batch]

    async def embed_and_upsert(
        self,
        graph: CodeGraph,
        mode: str,
        *,
        nodes_to_embed: Optional[list[Node]] = None,
        show_progress: bool = False,
    ) -> None:
        source = nodes_to_embed if nodes_to_embed is not None else list(graph.all_nodes())

        texts: list[str] = []
        nodes: list[Node] = []
        for node in source:
            if mode == "none" and not node.is_symbol:
                continue
            text = self.embedding_text(node, graph) if graph else (node.raw_code or node.summary or node.name)
            if text:
                texts.append(text[:4000])
                nodes.append(node)

        if not texts:
            if nodes_to_embed is None:
                logger.warning("No texts to embed")
            return

        logger.info("Embedding %d nodes...", len(texts))

        if show_progress:
            from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

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
                all_embeddings = await self.embed_texts_batched(
                    texts,
                    on_batch_done=lambda: progress.update(task_id, advance=1),
                )
        else:
            all_embeddings = await self.embed_texts_batched(texts)

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

        if self._vector_store is None:
            raise RuntimeError("Vector store is required for embed_and_upsert")

        self._vector_store.upsert(
            ids=ids,
            embeddings=all_embeddings,
            metadatas=metadatas,
            documents=texts,
        )

        logger.info("Embedded and stored %d vectors", len(ids))
