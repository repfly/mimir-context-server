"""Jina Embeddings v2 API client."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from mimir.domain.errors import EmbeddingError

logger = logging.getLogger(__name__)

_JINA_API_URL = "https://api.jina.ai/v1/embeddings"
_DEFAULT_MODEL = "jina-embeddings-v2-base-code"
_DIMENSION = 768  # jina-embeddings-v2-base-code output dimension


class JinaEmbedder:
    """Embedder backed by Jina Embeddings v2 API.

    Requires ``JINA_API_KEY`` environment variable or explicit api_key.
    """

    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        api_key: Optional[str] = None,
        api_key_env: Optional[str] = "JINA_API_KEY",
        batch_size: int = 64,
        max_concurrent: int = 5,
    ) -> None:
        self._model = model
        self._batch_size = batch_size
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._api_key = api_key or os.environ.get(api_key_env or "", "")
        if not self._api_key:
            raise EmbeddingError(
                f"Jina API key not found. Set {api_key_env} environment variable "
                f"or pass api_key directly."
            )

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed texts in batches with concurrency control."""
        all_embeddings: list[list[float]] = []

        # Split into batches
        batches = [
            texts[i : i + self._batch_size]
            for i in range(0, len(texts), self._batch_size)
        ]

        tasks = [self._embed_single_batch(batch) for batch in batches]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                raise EmbeddingError(f"Jina API batch failed: {result}") from result
            all_embeddings.extend(result)

        return all_embeddings

    async def _embed_single_batch(self, texts: list[str]) -> list[list[float]]:
        """Send a single batch to the Jina API."""
        import aiohttp

        async with self._semaphore:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        _JINA_API_URL,
                        json={
                            "model": self._model,
                            "input": texts,
                        },
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json",
                        },
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            raise EmbeddingError(
                                f"Jina API returned {resp.status}: {body[:500]}"
                            )
                        data = await resp.json()
                        # Sort by index to ensure correct ordering
                        sorted_data = sorted(data["data"], key=lambda x: x["index"])
                        return [item["embedding"] for item in sorted_data]
            except aiohttp.ClientError as exc:
                raise EmbeddingError(f"Jina API request failed: {exc}") from exc

    @property
    def dimension(self) -> int:
        return _DIMENSION
