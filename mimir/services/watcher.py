"""File watcher service — monitors repos for changes and triggers incremental re-indexing.

Uses ``watchdog`` to observe filesystem events, debounces rapid saves,
and schedules async graph updates on the MCP server's event loop.
All graph mutations happen on the asyncio event loop — the watchdog
thread only accumulates events and schedules flushes.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

if TYPE_CHECKING:
    from mimir.domain.config import MimirConfig
    from mimir.domain.graph import CodeGraph
    from mimir.ports.graph_store import GraphStore
    from mimir.ports.vector_store import VectorStore
    from mimir.services.indexing import IndexingService
    from mimir.services.retrieval import RetrievalService

logger = logging.getLogger(__name__)


class _DebouncingHandler(FileSystemEventHandler):
    """Accumulates filesystem events and schedules debounced flushes.

    Runs on watchdog's background thread.  Never touches the graph
    directly — only schedules work on the asyncio event loop.
    """

    def __init__(
        self,
        repo_name: str,
        repo_path: Path,
        excluded_patterns: list[str],
        supported_extensions: frozenset[str],
        debounce_ms: int,
        batch_window_ms: int,
        loop: asyncio.AbstractEventLoop,
        flush_callback,  # async callable(repo_name, changed, deleted)
    ) -> None:
        self._repo_name = repo_name
        self._repo_path = repo_path
        self._excluded_patterns = excluded_patterns
        self._supported_extensions = supported_extensions
        self._debounce_s = debounce_ms / 1000.0
        self._batch_window_s = batch_window_ms / 1000.0
        self._loop = loop
        self._flush_callback = flush_callback

        # Accumulated changes (thread-safe via lock)
        self._lock = threading.Lock()
        self._changed: set[str] = set()
        self._deleted: set[str] = set()
        self._batch_start: Optional[float] = None
        self._pending_handle: Optional[asyncio.TimerHandle] = None

    # -- watchdog callbacks (run on watchdog thread) -----------------------

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._record_change(event.src_path, deleted=False)

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._record_change(event.src_path, deleted=False)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._record_change(event.src_path, deleted=True)

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        # Treat as delete src + create dst
        self._record_change(event.src_path, deleted=True)
        if hasattr(event, 'dest_path') and event.dest_path:
            self._record_change(event.dest_path, deleted=False)

    # -- internal ----------------------------------------------------------

    def _record_change(self, abs_path: str, *, deleted: bool) -> None:
        """Record a file change and schedule a debounced flush."""
        # Check extension
        ext = os.path.splitext(abs_path)[1].lower()
        if ext not in self._supported_extensions:
            return

        # Compute relative path
        try:
            rel_path = os.path.relpath(abs_path, str(self._repo_path))
        except ValueError:
            return

        # Check exclusion patterns on each path component
        parts = Path(rel_path).parts
        for part in parts:
            if any(fnmatch.fnmatch(part, p) for p in self._excluded_patterns):
                return

        with self._lock:
            if deleted:
                self._deleted.add(rel_path)
                self._changed.discard(rel_path)
            else:
                self._changed.add(rel_path)
                self._deleted.discard(rel_path)

            now = time.monotonic()
            if self._batch_start is None:
                self._batch_start = now

            # Check if batch window exceeded — flush immediately
            if now - self._batch_start >= self._batch_window_s:
                self._schedule_flush_now()
            else:
                self._schedule_flush_debounced()

    def _schedule_flush_debounced(self) -> None:
        """Schedule a flush after debounce delay (cancel previous timer)."""
        if self._pending_handle is not None:
            self._pending_handle.cancel()

        self._pending_handle = self._loop.call_soon_threadsafe(
            self._set_timer,
        )

    def _schedule_flush_now(self) -> None:
        """Schedule an immediate flush on the event loop."""
        if self._pending_handle is not None:
            self._pending_handle.cancel()
            self._pending_handle = None

        self._loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(self._flush()),
        )

    def _set_timer(self) -> None:
        """Set a timer on the event loop (must be called from the event loop)."""
        if self._pending_handle is not None:
            self._pending_handle.cancel()

        self._pending_handle = self._loop.call_later(
            self._debounce_s, lambda: asyncio.ensure_future(self._flush()),
        )

    async def _flush(self) -> None:
        """Swap out accumulated changes and trigger re-indexing."""
        with self._lock:
            if not self._changed and not self._deleted:
                return

            changed = self._changed
            deleted = self._deleted
            self._changed = set()
            self._deleted = set()
            self._batch_start = None
            self._pending_handle = None

        logger.info(
            "Watcher flush for %s: %d changed, %d deleted",
            self._repo_name, len(changed), len(deleted),
        )

        await self._flush_callback(self._repo_name, changed, deleted)


class FileWatcherService:
    """Monitors configured repos for file changes and triggers live re-indexing.

    The observer runs on a background thread.  All graph mutations are
    scheduled on the provided asyncio event loop.
    """

    def __init__(
        self,
        config: MimirConfig,
        indexing_service: IndexingService,
        graph: CodeGraph,
        graph_store: GraphStore,
        vector_store: VectorStore,
        retrieval_service: RetrievalService,
    ) -> None:
        self._config = config
        self._indexing = indexing_service
        self._graph = graph
        self._graph_store = graph_store
        self._vector_store = vector_store
        self._retrieval = retrieval_service
        self._observer: Optional[Observer] = None
        self._update_lock = asyncio.Lock()

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start watching all configured repos."""
        if self._observer is not None:
            logger.warning("File watcher already running")
            return

        # Determine supported extensions from the parser
        supported = self._indexing._parser.supported_extensions()

        self._observer = Observer()
        watcher_cfg = self._config.watcher

        for repo_config in self._config.repos:
            repo_path = Path(repo_config.path)
            if not repo_path.is_dir():
                logger.warning("Watcher: repo path not found: %s", repo_path)
                continue

            handler = _DebouncingHandler(
                repo_name=repo_config.name,
                repo_path=repo_path,
                excluded_patterns=self._config.indexing.excluded_patterns,
                supported_extensions=supported,
                debounce_ms=watcher_cfg.debounce_ms,
                batch_window_ms=watcher_cfg.batch_window_ms,
                loop=loop,
                flush_callback=self._on_files_changed,
            )

            self._observer.schedule(handler, str(repo_path), recursive=True)
            logger.info("Watching repo: %s (%s)", repo_config.name, repo_path)

        self._observer.start()
        logger.info("File watcher started — monitoring %d repos", len(self._config.repos))

    def stop(self) -> None:
        """Stop the file watcher."""
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
            logger.info("File watcher stopped")

    async def _on_files_changed(
        self,
        repo_name: str,
        changed_files: set[str],
        deleted_files: set[str],
    ) -> None:
        """Called on the asyncio event loop when the debounce window expires."""
        async with self._update_lock:
            try:
                repo_config = next(
                    r for r in self._config.repos if r.name == repo_name
                )

                removed_ids, new_nodes, new_edges = await self._indexing.index_files(
                    graph=self._graph,
                    repo_name=repo_name,
                    repo_path=Path(repo_config.path),
                    changed_files=changed_files,
                    deleted_files=deleted_files,
                    language_hint=repo_config.language_hint,
                )

                # Persist changes
                if removed_ids:
                    self._graph_store.delete_nodes_by_ids(removed_ids)
                    self._vector_store.delete(removed_ids)

                if new_nodes or new_edges:
                    self._graph_store.save_partial(new_nodes, new_edges)

                # Invalidate BM25 index so it's rebuilt on next search
                self._retrieval.invalidate_bm25()

                logger.info(
                    "Watcher update for %s: -%d removed, +%d nodes, +%d edges",
                    repo_name,
                    len(removed_ids),
                    len(new_nodes),
                    len(new_edges),
                )
            except StopIteration:
                logger.error("Watcher: repo %s not found in config", repo_name)
            except Exception:
                logger.exception("Watcher re-index failed for %s", repo_name)
