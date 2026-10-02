"""Local embedder running sentence-transformers models via ONNX Runtime (zero-cost, offline).

Loads the ONNX export that sentence-transformers model repos ship under
``onnx/model.onnx`` and reproduces the sentence-transformers pipeline
(tokenize → transformer → pooling → optional normalize) from the repo's own
``modules.json`` / ``1_Pooling/config.json``. No torch required.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

import numpy as np

from mimir.domain.errors import EmbeddingError

logger = logging.getLogger(__name__)

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
for _noisy in ("huggingface_hub", "huggingface_hub.utils._http", "httpx"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

_MODEL_FILES = [
    "onnx/model.onnx",
    "onnx/model.onnx_data",
    "tokenizer.json",
    "config.json",
    "modules.json",
    "sentence_bert_config.json",
    "1_Pooling/config.json",
]

# Texts per ONNX forward pass. Texts are length-sorted first so each chunk
# pads to a similar length.
_CHUNK_SIZE = 32


def _repo_id(model_name: str) -> str:
    """Map short names (``all-MiniLM-L6-v2``, ``jina-embeddings-v2-base-code``) to repo ids."""
    if "/" in model_name:
        return model_name
    owner = "jinaai" if model_name.startswith("jina-") else "sentence-transformers"
    return f"{owner}/{model_name}"


class LocalEmbedder:
    """Embedder for sentence-transformers models, runs fully offline on CPU.

    Default model: ``all-MiniLM-L6-v2`` (384 dimensions, ~90MB).
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2", cache_dir: Optional[str] = None) -> None:
        self._model_name = model_name
        self._cache_dir = cache_dir
        self._session = None
        self._tokenizer = None
        self._input_names: set[str] = set()
        self._pooling = "mean"
        self._normalize = False
        self._dim: Optional[int] = None

    def _download(self) -> Path:
        from huggingface_hub import snapshot_download

        repo = _repo_id(self._model_name)
        # Prefer local copies: the configured cache, then the default HF cache
        # (where the Docker image pre-bakes the model). Download only if neither has it.
        for cache_dir in dict.fromkeys([self._cache_dir, None]):
            try:
                path = Path(snapshot_download(
                    repo, allow_patterns=_MODEL_FILES, cache_dir=cache_dir, local_files_only=True,
                ))
                if (path / "onnx" / "model.onnx").is_file():
                    return path
            except Exception:
                continue
        logger.info("Model '%s' not cached — downloading for the first time...", repo)
        path = Path(snapshot_download(repo, allow_patterns=_MODEL_FILES, cache_dir=self._cache_dir))
        if not (path / "onnx" / "model.onnx").is_file():
            raise EmbeddingError(f"Model '{repo}' has no ONNX export (onnx/model.onnx)")
        return path

    def _ensure_model(self) -> None:
        if self._session is not None:
            return
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer

            path = self._download()

            def _read(name: str) -> dict:
                f = path / name
                return json.loads(f.read_text()) if f.is_file() else {}

            modules = _read("modules.json")
            pooling_cfg = _read("1_Pooling/config.json")
            max_len = _read("sentence_bert_config.json").get("max_seq_length") or 512
            max_len = min(max_len, _read("config.json").get("max_position_embeddings") or max_len)

            if pooling_cfg.get("pooling_mode_cls_token"):
                self._pooling = "cls"
            elif pooling_cfg.get("pooling_mode_max_tokens"):
                self._pooling = "max"
            else:
                self._pooling = "mean"
            self._normalize = any(m.get("type", "").endswith("Normalize") for m in modules)

            tokenizer = Tokenizer.from_file(str(path / "tokenizer.json"))
            tokenizer.enable_truncation(max_length=max_len)
            pad = tokenizer.padding or {}
            tokenizer.enable_padding(
                pad_id=pad.get("pad_id", 0), pad_token=pad.get("pad_token", "[PAD]"),
            )
            self._tokenizer = tokenizer

            opts = ort.SessionOptions()
            opts.log_severity_level = 3
            self._session = ort.InferenceSession(
                str(path / "onnx" / "model.onnx"), opts, providers=["CPUExecutionProvider"],
            )
            self._input_names = {i.name for i in self._session.get_inputs()}

            self._dim = len(self._encode(["test"])[0])
            logger.info("Loaded local embedding model: %s (dim=%d)", self._model_name, self._dim)
        except EmbeddingError:
            raise
        except Exception as exc:
            self._session = None
            raise EmbeddingError(f"Failed to load model '{self._model_name}': {exc}") from exc

    def _encode(self, texts: list[str]) -> np.ndarray:
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        out = np.empty((len(texts), 0), dtype=np.float32)
        for start in range(0, len(order), _CHUNK_SIZE):
            idx = order[start:start + _CHUNK_SIZE]
            encs = self._tokenizer.encode_batch([texts[i] for i in idx])  # type: ignore[union-attr]
            mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
            feeds = {
                "input_ids": np.array([e.ids for e in encs], dtype=np.int64),
                "attention_mask": mask,
                "token_type_ids": np.array([e.type_ids for e in encs], dtype=np.int64),
            }
            feeds = {k: v for k, v in feeds.items() if k in self._input_names}
            tokens = self._session.run(None, feeds)[0]  # type: ignore[union-attr]  # (batch, seq, dim)

            if self._pooling == "cls":
                pooled = tokens[:, 0]
            elif self._pooling == "max":
                pooled = np.where(mask[..., None] > 0, tokens, -1e9).max(axis=1)
            else:
                m = mask[..., None].astype(np.float32)
                pooled = (tokens * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)
            if self._normalize:
                pooled = pooled / np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None)

            if out.shape[1] == 0:
                out = np.empty((len(texts), pooled.shape[1]), dtype=np.float32)
            out[idx] = pooled
        return out

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self._ensure_model()
        if not texts:
            return []
        try:
            # ONNX inference is synchronous and CPU bound. Offload to a worker
            # thread so callers (HTTP/MCP handlers running on the same loop)
            # stay responsive during indexing.
            embeddings = await asyncio.to_thread(self._encode, texts)
            return embeddings.tolist()
        except Exception as exc:
            raise EmbeddingError(f"Local embedding failed: {exc}") from exc

    @property
    def dimension(self) -> int:
        self._ensure_model()
        assert self._dim is not None
        return self._dim
