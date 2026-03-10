"""Configuration dataclasses — parsed from ``mimir.toml``.

Every field is validated eagerly on construction.  ``ConfigError``
is raised for missing keys, bad values, or unreachable paths.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

from mimir.domain.errors import ConfigError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sub-configs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RepoConfig:
    name: str
    path: Path
    language_hint: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.path.is_dir():
            raise ConfigError(
                f"Repo '{self.name}' path does not exist or is not a directory: {self.path}"
            )


@dataclass(frozen=True)
class CrossRepoConfig:
    detect_api_contracts: bool = True
    detect_shared_imports: bool = True
    proto_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class IndexingConfig:
    summary_mode: Literal["none", "heuristic", "llm"] = "heuristic"
    excluded_patterns: list[str] = field(default_factory=lambda: [
        "*.test.*", "*.spec.*", "__pycache__", "node_modules", ".git", "venv",
        ".venv", "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
    ])
    max_file_size_kb: int = 500
    concurrency: int = 10

    def __post_init__(self) -> None:
        if self.summary_mode not in ("none", "heuristic", "llm"):
            raise ConfigError(f"Invalid summary_mode: {self.summary_mode!r}")
        if self.max_file_size_kb <= 0:
            raise ConfigError("max_file_size_kb must be positive")
        if self.concurrency <= 0:
            raise ConfigError("concurrency must be positive")


@dataclass(frozen=True)
class LlmConfig:
    model: str = "claude-haiku-4-5-20251001"
    api_key_env: Optional[str] = None
    api_base: Optional[str] = None


@dataclass(frozen=True)
class EmbeddingConfig:
    model: str = "jina-embeddings-v2-base-code"
    api_key_env: Optional[str] = None
    batch_size: int = 64
    cache_dir: Optional[str] = None

    def __post_init__(self) -> None:
        if self.batch_size <= 0:
            raise ConfigError("embedding batch_size must be positive")


@dataclass(frozen=True)
class VectorDbConfig:
    backend: Literal["chroma", "numpy"] = "numpy"
    persist_directory: Optional[str] = None


@dataclass(frozen=True)
class RetrievalConfig:
    default_beam_width: int = 3
    default_token_budget: int = 8000
    expansion_hops: int = 2
    hybrid_alpha: float = 0.7
    relevance_gate: float = 0.3

    def __post_init__(self) -> None:
        if self.default_beam_width <= 0:
            raise ConfigError("beam_width must be positive")
        if self.default_token_budget <= 0:
            raise ConfigError("token_budget must be positive")
        if not 0.0 <= self.hybrid_alpha <= 1.0:
            raise ConfigError("hybrid_alpha must be between 0.0 and 1.0")


@dataclass(frozen=True)
class TemporalConfig:
    recency_lambda: float = 0.02
    change_window_commits: int = 100
    co_retrieval_enabled: bool = True


@dataclass(frozen=True)
class SessionConfig:
    context_decay_turns: int = 5
    topic_tracking_alpha: float = 0.3


# ---------------------------------------------------------------------------
# Root config
# ---------------------------------------------------------------------------

@dataclass
class MimirConfig:
    """Root configuration — assembled from ``mimir.toml``."""

    repos: list[RepoConfig]
    data_dir: Path = field(default_factory=lambda: Path(".mimir"))

    cross_repo: CrossRepoConfig = field(default_factory=CrossRepoConfig)
    indexing: IndexingConfig = field(default_factory=IndexingConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    embeddings: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    vector_db: VectorDbConfig = field(default_factory=VectorDbConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    temporal: TemporalConfig = field(default_factory=TemporalConfig)
    session: SessionConfig = field(default_factory=SessionConfig)

    def __post_init__(self) -> None:
        if not self.repos:
            raise ConfigError("At least one repo must be configured")
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def load(cls, path: Path) -> MimirConfig:
        """Parse a ``mimir.toml`` file into a validated config."""
        import tomli

        if not path.is_file():
            raise ConfigError(f"Config file not found: {path}")

        with open(path, "rb") as f:
            raw = tomli.load(f)

        try:
            repos = [
                RepoConfig(
                    name=r["name"],
                    path=path.parent.joinpath(Path(r["path"]).expanduser()).resolve(),
                    language_hint=r.get("language_hint"),
                )
                for r in raw.get("repos", [])
            ]

            data_dir = path.parent.joinpath(Path(raw.get("data_dir", ".mimir")).expanduser()).resolve()

            # Resolve relative paths in sub-configs against config file dir
            config_dir = path.parent

            vector_db = _parse_section(VectorDbConfig, raw.get("vector_db", {}))
            if vector_db.persist_directory and not Path(vector_db.persist_directory).is_absolute():
                object.__setattr__(
                    vector_db, "persist_directory",
                    str(config_dir.joinpath(vector_db.persist_directory).resolve()),
                )

            embeddings = _parse_section(EmbeddingConfig, raw.get("embeddings", {}))
            if embeddings.cache_dir and not Path(embeddings.cache_dir).is_absolute():
                object.__setattr__(
                    embeddings, "cache_dir",
                    str(config_dir.joinpath(embeddings.cache_dir).resolve()),
                )

            return cls(
                repos=repos,
                data_dir=data_dir,
                cross_repo=_parse_section(CrossRepoConfig, raw.get("cross_repo", {})),
                indexing=_parse_section(IndexingConfig, raw.get("indexing", {})),
                llm=_parse_section(LlmConfig, raw.get("llm", {})),
                embeddings=embeddings,
                vector_db=vector_db,
                retrieval=_parse_section(RetrievalConfig, raw.get("retrieval", {})),
                temporal=_parse_section(TemporalConfig, raw.get("temporal", {})),
                session=_parse_section(SessionConfig, raw.get("session", {})),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"Invalid config: {exc}") from exc


def _parse_section(cls: type, data: dict) -> object:
    """Instantiate a frozen dataclass from a dict, dropping unknown keys."""
    import dataclasses

    known_fields = {f.name for f in dataclasses.fields(cls)}
    filtered = {k: v for k, v in data.items() if k in known_fields}
    unknown = set(data.keys()) - known_fields
    if unknown:
        logger.warning("Unknown config keys in %s: %s", cls.__name__, unknown)
    return cls(**filtered)
