"""Tests for IndexingService._embed_texts_batched.

Covers:
- Order preservation: flattened result lines up with input texts regardless of
  how they get chunked across batches.
- max_concurrent_batches > 1 actually allows batches to overlap in-flight.
- max_concurrent_batches == 1 serializes batches (no overlap).
- The embedding path yields to the event loop (``asyncio.to_thread``-style
  embedders stay off the loop and let sentinel tasks tick).

``IndexingService`` is constructed via ``__new__`` so we can inject a fake
embedder and a duck-typed config without standing up the full DI container.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from mimir.services.indexing import IndexingService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(*, batch_size: int, max_concurrent_batches: int, embedder) -> IndexingService:
    """Build a bare IndexingService with just the fields _embed_texts_batched touches."""
    service = IndexingService.__new__(IndexingService)
    service._config = SimpleNamespace(
        embeddings=SimpleNamespace(
            batch_size=batch_size,
            max_concurrent_batches=max_concurrent_batches,
        ),
    )
    service._embedder = embedder
    return service


class _OrderedEmbedder:
    """Deterministic embedder: each text becomes a single-element vector equal
    to a running index, so the flattened output encodes input order exactly.
    """

    def __init__(self) -> None:
        self.batches_seen: list[list[str]] = []

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.batches_seen.append(list(texts))
        return [[float(int(t))] for t in texts]


class _OverlapTrackingEmbedder:
    """Tracks the maximum number of concurrent ``embed_batch`` calls.

    A short sleep is used to create a window in which overlap, if allowed by
    the semaphore, becomes observable on any event loop.
    """

    def __init__(self, sleep: float = 0.05) -> None:
        self.sleep = sleep
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = asyncio.Lock()

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        async with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.sleep)
            return [[0.0] for _ in texts]
        finally:
            async with self._lock:
                self.in_flight -= 1


class _ThreadedEmbedder:
    """Mimics LocalEmbedder's post-fix shape: the hot call runs in a worker
    thread via ``asyncio.to_thread``, so the event loop stays free to tick
    other coroutines while embedding is in progress.
    """

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        import time
        def _blocking_encode(items: list[str]) -> list[list[float]]:
            time.sleep(0.05)
            return [[0.0] for _ in items]
        return await asyncio.to_thread(_blocking_encode, texts)


# ---------------------------------------------------------------------------
# Order preservation
# ---------------------------------------------------------------------------


async def test_order_is_preserved_across_batches() -> None:
    embedder = _OrderedEmbedder()
    service = _make_service(batch_size=2, max_concurrent_batches=4, embedder=embedder)

    # 5 inputs → 3 chunks of sizes [2, 2, 1] when batch_size=2.
    texts = ["0", "1", "2", "3", "4"]
    result = await service._embed_texts_batched(texts)

    assert result == [[0.0], [1.0], [2.0], [3.0], [4.0]]
    assert embedder.batches_seen == [["0", "1"], ["2", "3"], ["4"]]


async def test_empty_input_returns_empty_list() -> None:
    embedder = _OrderedEmbedder()
    service = _make_service(batch_size=8, max_concurrent_batches=2, embedder=embedder)

    result = await service._embed_texts_batched([])

    assert result == []
    assert embedder.batches_seen == []


async def test_single_batch_when_input_smaller_than_batch_size() -> None:
    embedder = _OrderedEmbedder()
    service = _make_service(batch_size=16, max_concurrent_batches=4, embedder=embedder)

    result = await service._embed_texts_batched(["0", "1", "2"])

    assert result == [[0.0], [1.0], [2.0]]
    assert len(embedder.batches_seen) == 1


# ---------------------------------------------------------------------------
# Concurrency semantics
# ---------------------------------------------------------------------------


async def test_max_concurrent_gt_1_allows_overlap() -> None:
    embedder = _OverlapTrackingEmbedder(sleep=0.05)
    service = _make_service(batch_size=1, max_concurrent_batches=3, embedder=embedder)

    # 4 batches × sleep 50ms, semaphore=3 → at least 2 observed in flight.
    await service._embed_texts_batched(["a", "b", "c", "d"])

    assert embedder.max_in_flight >= 2
    assert embedder.max_in_flight <= 3


async def test_max_concurrent_eq_1_serializes_batches() -> None:
    embedder = _OverlapTrackingEmbedder(sleep=0.02)
    service = _make_service(batch_size=1, max_concurrent_batches=1, embedder=embedder)

    await service._embed_texts_batched(["a", "b", "c", "d"])

    assert embedder.max_in_flight == 1


# ---------------------------------------------------------------------------
# Event-loop responsiveness
# ---------------------------------------------------------------------------


async def test_threaded_embedder_does_not_block_event_loop() -> None:
    """A LocalEmbedder-style ``asyncio.to_thread`` embedder must let other
    coroutines progress while it embeds.  We verify by racing a sentinel task
    that ticks every few milliseconds — if the loop is blocked, it won't
    accumulate ticks during the embed window.
    """
    embedder = _ThreadedEmbedder()
    service = _make_service(batch_size=2, max_concurrent_batches=1, embedder=embedder)

    ticks = 0

    async def sentinel() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    sentinel_task = asyncio.create_task(sentinel())
    try:
        # 4 inputs × batch_size=2 → 2 sequential batches × 50ms = ~100ms total.
        await service._embed_texts_batched(["0", "1", "2", "3"])
    finally:
        sentinel_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sentinel_task

    # 100ms of embedding @ 5ms sentinel ticks → comfortably ≥ 5 ticks if the
    # loop stays responsive.  On a blocked loop this would be 0–1.
    assert ticks >= 5
