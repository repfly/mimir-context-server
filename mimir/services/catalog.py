"""Catalog service — generates Backstage-compatible service catalog data.

Transforms the code graph into structured catalog entries that can be
consumed by Backstage entity providers, CI/CD tools, or any service
catalog system.  Also provides drift detection to compare declared
dependencies against code-analyzed reality.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from mimir.domain.config import MimirConfig

from mimir.domain.catalog import (
    CatalogApi,
    CatalogResponse,
    CatalogServiceEntry,
    DriftEntry,
    DriftReport,
    ServiceDependency,
    TechStack,
)
from mimir.domain.graph import CodeGraph
from mimir.domain.lang import detect_language
from mimir.domain.models import EdgeKind, NodeKind
from mimir.services.quality import QualityService

logger = logging.getLogger(__name__)

# Well-known framework indicators: import target → display name
_FRAMEWORK_INDICATORS: dict[str, str] = {
    "flask": "Flask",
    "fastapi": "FastAPI",
    "django": "Django",
    "aiohttp": "aiohttp",
    "starlette": "Starlette",
    "tornado": "Tornado",
    "express": "Express.js",
    "koa": "Koa",
    "nestjs": "NestJS",
    "react": "React",
    "nextjs": "Next.js",
    "vue": "Vue",
    "angular": "Angular",
    "svelte": "Svelte",
    "spring": "Spring",
    "quarkus": "Quarkus",
    "gin": "Gin",
    "fiber": "Fiber",
    "echo": "Echo",
    "actix": "Actix",
    "axum": "Axum",
    "rocket": "Rocket",
    "rails": "Rails",
    "sinatra": "Sinatra",
    "vapor": "Vapor",
    "sqlalchemy": "SQLAlchemy",
    "prisma": "Prisma",
    "sequelize": "Sequelize",
    "celery": "Celery",
    "pytest": "pytest",
    "junit": "JUnit",
}


class CatalogService:
    """Generates catalog data from the code graph."""

    def __init__(
        self,
        *,
        quality_service: QualityService,
        config: Optional[MimirConfig] = None,
    ) -> None:
        self._quality = quality_service
        self._config = config
        self._owner_cache: dict[str, str] = {}

    def generate_catalog(
        self,
        graph: CodeGraph,
        repos: Optional[list[str]] = None,
    ) -> CatalogResponse:
        """Build a full catalog response from the code graph."""
        self._owner_cache.clear()
        target_repos = repos if repos else graph.repos
        cross_repo_edges = graph.cross_repo_edges()

        services: list[CatalogServiceEntry] = []
        for repo_name in target_repos:
            entry = self._build_service_entry(
                graph, repo_name, cross_repo_edges,
            )
            services.append(entry)

        return CatalogResponse(
            services=tuple(services),
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    def detect_drift(
        self,
        graph: CodeGraph,
        repo: str,
        declared_deps: list[dict[str, str]],
    ) -> DriftReport:
        """Compare declared dependencies against code-analyzed reality."""
        # Build actual dependency set from cross-repo edges
        actual_deps: dict[str, list[dict[str, str]]] = defaultdict(list)
        for edge in graph.cross_repo_edges():
            src = graph.get_node(edge.source)
            tgt = graph.get_node(edge.target)
            if not src or not tgt:
                continue
            if src.repo == repo:
                actual_deps[tgt.repo].append({
                    "source_node": edge.source,
                    "target_node": edge.target,
                    "type": edge.kind.value,
                })

        declared_names = {d["name"] for d in declared_deps}
        actual_names = set(actual_deps.keys())

        confirmed: list[DriftEntry] = []
        missing_in_code: list[DriftEntry] = []
        undeclared: list[DriftEntry] = []

        for name in declared_names & actual_names:
            confirmed.append(DriftEntry(
                dependency=name,
                status="confirmed",
                evidence=tuple(actual_deps[name]),
            ))

        for name in declared_names - actual_names:
            missing_in_code.append(DriftEntry(
                dependency=name,
                status="missing_in_code",
            ))

        for name in actual_names - declared_names:
            undeclared.append(DriftEntry(
                dependency=name,
                status="undeclared_in_catalog",
                evidence=tuple(actual_deps[name]),
            ))

        total = len(confirmed) + len(missing_in_code) + len(undeclared)
        drift_score = (
            (len(missing_in_code) + len(undeclared)) / max(total, 1)
        )

        return DriftReport(
            repo=repo,
            confirmed=tuple(confirmed),
            missing_in_code=tuple(missing_in_code),
            undeclared=tuple(undeclared),
            drift_score=drift_score,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_service_entry(
        self,
        graph: CodeGraph,
        repo_name: str,
        cross_repo_edges: list,
    ) -> CatalogServiceEntry:
        """Build a single CatalogServiceEntry for a repo."""
        # Find the REPOSITORY node
        repo_node_id = ""
        for node in graph.nodes_by_kind(NodeKind.REPOSITORY):
            if node.repo == repo_name:
                repo_node_id = node.id
                break

        apis = self._discover_apis(graph, repo_name)
        deps, dependents = self._build_dependency_map(
            graph, repo_name, cross_repo_edges,
        )
        tech_stack = self._aggregate_tech_stack(graph, repo_name)
        quality_score, quality_dist = self._compute_repo_quality(
            graph, repo_name,
        )
        node_counts = self._count_nodes_by_kind(graph, repo_name)

        owner = self._infer_repo_owner(repo_name)

        return CatalogServiceEntry(
            repo=repo_name,
            node_id=repo_node_id,
            apis=tuple(apis),
            dependencies=tuple(deps),
            dependents=tuple(dependents),
            tech_stack=tech_stack,
            quality_score=quality_score,
            quality_distribution=quality_dist,
            node_counts=node_counts,
            owner=owner,
        )

    def _discover_apis(
        self, graph: CodeGraph, repo_name: str,
    ) -> list[CatalogApi]:
        """Find all API endpoints in a repo."""
        from mimir.services.indexing import IndexingService

        apis: list[CatalogApi] = []
        for node in graph.nodes_by_kind(NodeKind.API_ENDPOINT):
            if node.repo != repo_name:
                continue

            method = "GET"
            path = ""

            # Extract method/path from raw_code decorators
            if node.raw_code:
                for line in node.raw_code.split("\n"):
                    line = line.strip()
                    if line.startswith("@"):
                        info = IndexingService._parse_endpoint_decorator(line)
                        if info:
                            method = info.get("method", "GET").upper()
                            path = info.get("endpoint", "")
                            break

            apis.append(CatalogApi(
                node_id=node.id,
                path=path,
                method=method,
                containing_function=node.name,
                repo=repo_name,
            ))

        return apis

    def _build_dependency_map(
        self,
        graph: CodeGraph,
        repo_name: str,
        cross_repo_edges: list,
    ) -> tuple[list[ServiceDependency], list[ServiceDependency]]:
        """Build outgoing (dependencies) and incoming (dependents) lists."""
        # Group edges by (source_repo, target_repo, edge_kind)
        outgoing: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
        incoming: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)

        for edge in cross_repo_edges:
            src = graph.get_node(edge.source)
            tgt = graph.get_node(edge.target)
            if not src or not tgt:
                continue

            evidence = {
                "source_node": edge.source,
                "target_node": edge.target,
            }

            if src.repo == repo_name and tgt.repo != repo_name:
                key = (tgt.repo, edge.kind.value)
                outgoing[key].append(evidence)
            elif tgt.repo == repo_name and src.repo != repo_name:
                key = (src.repo, edge.kind.value)
                incoming[key].append(evidence)

        deps = [
            ServiceDependency(
                source_repo=repo_name,
                target_repo=target_repo,
                dependency_type=dep_type,
                evidence=tuple(evs),
            )
            for (target_repo, dep_type), evs in outgoing.items()
        ]

        dependents = [
            ServiceDependency(
                source_repo=source_repo,
                target_repo=repo_name,
                dependency_type=dep_type,
                evidence=tuple(evs),
            )
            for (source_repo, dep_type), evs in incoming.items()
        ]

        return deps, dependents

    def _aggregate_tech_stack(
        self, graph: CodeGraph, repo_name: str,
    ) -> TechStack:
        """Detect languages, frameworks, and key dependencies."""
        lang_counts: dict[str, int] = defaultdict(int)
        for node in graph.nodes_by_kind(NodeKind.FILE):
            if node.repo != repo_name:
                continue
            lang = detect_language(node.path)
            if lang:
                lang_counts[lang] += 1

        # Detect frameworks from IMPORTS edges
        frameworks: set[str] = set()
        dep_counts: dict[str, int] = defaultdict(int)
        for node in graph.nodes_by_repo(repo_name):
            for edge in graph.get_outgoing_edges(node.id, kind=EdgeKind.IMPORTS):
                target = graph.get_node(edge.target)
                if not target:
                    continue
                target_name = target.name.lower()
                # Check against known frameworks
                for indicator, display_name in _FRAMEWORK_INDICATORS.items():
                    if indicator in target_name:
                        frameworks.add(display_name)
                dep_counts[target.name] += 1

        # Top dependencies by import count
        top_deps = sorted(dep_counts, key=dep_counts.get, reverse=True)[:10]

        return TechStack(
            languages=dict(lang_counts),
            frameworks=tuple(sorted(frameworks)),
            key_dependencies=tuple(top_deps),
        )

    def _compute_repo_quality(
        self, graph: CodeGraph, repo_name: str,
    ) -> tuple[float, dict[str, int]]:
        """Average quality score and distribution for a repo."""
        scores: list[float] = []
        dist: dict[str, int] = {"good": 0, "moderate": 0, "poor": 0}

        for node in graph.nodes_by_repo(repo_name):
            score = self._quality.compute_quality_score(node, graph)
            scores.append(score)
            if score >= 0.7:
                dist["good"] += 1
            elif score >= 0.4:
                dist["moderate"] += 1
            else:
                dist["poor"] += 1

        avg = sum(scores) / len(scores) if scores else 0.0
        return avg, dist

    def _count_nodes_by_kind(
        self, graph: CodeGraph, repo_name: str,
    ) -> dict[str, int]:
        """Count nodes grouped by kind for a repo."""
        counts: dict[str, int] = defaultdict(int)
        for node in graph.nodes_by_repo(repo_name):
            counts[node.kind.value] += 1
        return dict(counts)

    def _infer_repo_owner(self, repo_name: str) -> str:
        """Infer the primary owner of a repo from git commit history.

        Returns a Backstage-compatible owner ref like ``"user:dev@example.com"``
        or ``"unknown"`` if ownership cannot be determined.
        """
        if repo_name in self._owner_cache:
            return self._owner_cache[repo_name]

        owner = "unknown"
        if not self._config:
            self._owner_cache[repo_name] = owner
            return owner

        # Find the matching RepoConfig to get the filesystem path
        repo_config = None
        for rc in self._config.repos:
            if rc.name == repo_name:
                repo_config = rc
                break

        if not repo_config:
            self._owner_cache[repo_name] = owner
            return owner

        try:
            import git as gitmodule
            repo = gitmodule.Repo(str(repo_config.path))
            commits = list(repo.iter_commits(max_count=200))
            if commits:
                counts = Counter(c.author.email for c in commits)
                top_email, _ = counts.most_common(1)[0]
                owner = f"user:{top_email}"
        except Exception:
            logger.debug("Could not infer owner for %s", repo_name, exc_info=True)

        self._owner_cache[repo_name] = owner
        return owner
