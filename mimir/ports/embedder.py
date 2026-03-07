"""Embedder port — interface for computing text embeddings."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Embedder(Protocol):
    """Interface for embedding text into dense vectors.

    Implementations: ``JinaEmbedder``, ``LocalEmbedder``.
    """

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts.

        Parameters
        ----------
        texts
            List of texts to embed.

        Returns
        -------
        list[list[float]]
            One embedding vector per input text.

        Raises
        ------
        EmbeddingError
            If the embedding API call fails.
        """
        ...

    @property
    def dimension(self) -> int:
        """Dimensionality of the embedding vectors."""
        ...
