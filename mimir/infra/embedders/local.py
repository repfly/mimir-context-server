"""Local embedder using sentence-transformers (zero-cost, offline)."""

from __future__ import annotations

import logging
import os
from typing import Optional

from mimir.domain.errors import EmbeddingError

logger = logging.getLogger(__name__)

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
for _noisy in (
    "sentence_transformers",
    "sentence_transformers.models.transformer",
    "huggingface_hub",
    "huggingface_hub.utils._http",
    "transformers",
    "transformers.utils.loading_report",
    "httpx",
):
    logging.getLogger(_noisy).setLevel(logging.ERROR)


class LocalEmbedder:
    """Embedder backed by sentence-transformers, runs fully offline.

    Default model: ``all-MiniLM-L6-v2`` (384 dimensions, ~80MB).
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2", cache_dir: Optional[str] = None) -> None:
        self._model_name = model_name
        self._cache_dir = cache_dir
        self._model: Optional[object] = None
        self._dim: Optional[int] = None

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self._model_name, cache_folder=self._cache_dir)
            # Probe dimension quietly
            test = self._model.encode(["test"], show_progress_bar=False)
            self._dim = len(test[0])
            logger.info("Loaded local embedding model: %s (dim=%d)", self._model_name, self._dim)
        except ImportError:
            raise EmbeddingError(
                "sentence-transformers not installed. "
                "Install with: pip install sentence-transformers"
            )
        except Exception as exc:
            raise EmbeddingError(f"Failed to load model '{self._model_name}': {exc}") from exc

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self._ensure_model()
        try:
            embeddings = self._model.encode(texts, show_progress_bar=False)  # type: ignore[union-attr]
            return [emb.tolist() for emb in embeddings]
        except Exception as exc:
            raise EmbeddingError(f"Local embedding failed: {exc}") from exc

    @property
    def dimension(self) -> int:
        self._ensure_model()
        assert self._dim is not None
        return self._dim
