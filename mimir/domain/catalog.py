"""Domain models for the Backstage catalog integration.

These frozen dataclasses represent the structured output of Mimir's
code graph analysis, formatted for consumption by Backstage entity
providers and other service catalog tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, unique
from typing import Any


@unique
class DriftStatus(str, Enum):
    """Status of a dependency drift entry."""

    CONFIRMED = "confirmed"
    MISSING_IN_CODE = "missing_in_code"
    UNDECLARED_IN_CATALOG = "undeclared_in_catalog"


@dataclass(frozen=True)
class CatalogApi:
    """A discovered API endpoint in the code graph."""

    node_id: str
    path: str
    method: str
    containing_function: str
    repo: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "path": self.path,
            "method": self.method,
            "containing_function": self.containing_function,
            "repo": self.repo,
        }


@dataclass(frozen=True)
class ServiceDependency:
    """A cross-repo dependency between two services."""

    source_repo: str
    target_repo: str
    dependency_type: str  # "api_calls" | "shared_lib" | "imports"
    evidence: tuple[dict[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_repo": self.source_repo,
            "target_repo": self.target_repo,
            "dependency_type": self.dependency_type,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class TechStack:
    """Detected technology stack for a service."""

    languages: dict[str, int] = field(default_factory=dict)
    frameworks: tuple[str, ...] = ()
    key_dependencies: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "languages": dict(self.languages),
            "frameworks": list(self.frameworks),
            "key_dependencies": list(self.key_dependencies),
        }


@dataclass(frozen=True)
class CatalogServiceEntry:
    """A single service (repo) in the catalog."""

    repo: str
    node_id: str
    apis: tuple[CatalogApi, ...] = ()
    dependencies: tuple[ServiceDependency, ...] = ()
    dependents: tuple[ServiceDependency, ...] = ()
    tech_stack: TechStack = field(default_factory=TechStack)
    quality_score: float = 0.0
    quality_distribution: dict[str, int] = field(default_factory=dict)
    node_counts: dict[str, int] = field(default_factory=dict)
    owner: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "node_id": self.node_id,
            "owner": self.owner,
            "apis": [a.to_dict() for a in self.apis],
            "dependencies": [d.to_dict() for d in self.dependencies],
            "dependents": [d.to_dict() for d in self.dependents],
            "tech_stack": self.tech_stack.to_dict(),
            "quality_score": round(self.quality_score, 3),
            "quality_distribution": dict(self.quality_distribution),
            "node_counts": dict(self.node_counts),
        }


@dataclass(frozen=True)
class CatalogResponse:
    """Full catalog response containing all discovered services."""

    services: tuple[CatalogServiceEntry, ...] = ()
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "services": [s.to_dict() for s in self.services],
            "generated_at": self.generated_at,
        }

    def format_for_llm(self) -> str:
        parts: list[str] = []
        parts.append(f"# Service Catalog ({len(self.services)} services)")
        if self.generated_at:
            parts.append(f"Generated: {self.generated_at}\n")

        for svc in self.services:
            svc_type = "service" if svc.apis else "library"
            parts.append(f"## {svc.repo} ({svc_type})")
            parts.append(f"Owner: {svc.owner} | Quality: {svc.quality_score:.2f}")

            langs = ", ".join(
                f"{lang} ({count})" for lang, count in svc.tech_stack.languages.items()
            )
            if langs:
                parts.append(f"Languages: {langs}")
            if svc.tech_stack.frameworks:
                parts.append(f"Frameworks: {', '.join(svc.tech_stack.frameworks)}")

            if svc.apis:
                parts.append("APIs:")
                for api in svc.apis:
                    parts.append(f"  - {api.method} {api.path} ({api.containing_function})")

            if svc.dependencies:
                deps = ", ".join(
                    f"{d.target_repo} ({d.dependency_type})" for d in svc.dependencies
                )
                parts.append(f"Dependencies: {deps}")

            if svc.dependents:
                deps = ", ".join(
                    f"{d.source_repo} ({d.dependency_type})" for d in svc.dependents
                )
                parts.append(f"Dependents: {deps}")

            parts.append("")

        return "\n".join(parts)


@dataclass(frozen=True)
class DriftEntry:
    """A single dependency drift finding."""

    dependency: str
    status: DriftStatus
    evidence: tuple[dict[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "dependency": self.dependency,
            "status": self.status.value,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class DriftReport:
    """Comparison of declared vs. code-analyzed dependencies."""

    repo: str
    confirmed: tuple[DriftEntry, ...] = ()
    missing_in_code: tuple[DriftEntry, ...] = ()
    undeclared: tuple[DriftEntry, ...] = ()
    drift_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "confirmed": [e.to_dict() for e in self.confirmed],
            "missing_in_code": [e.to_dict() for e in self.missing_in_code],
            "undeclared": [e.to_dict() for e in self.undeclared],
            "drift_score": round(self.drift_score, 3),
        }

    def format_for_llm(self) -> str:
        parts: list[str] = []
        parts.append(f"# Dependency Drift Report: {self.repo}")
        parts.append(f"Drift score: {self.drift_score:.1%}\n")

        if self.confirmed:
            parts.append(f"## Confirmed ({len(self.confirmed)})")
            for e in self.confirmed:
                parts.append(f"  - {e.dependency}")

        if self.undeclared:
            parts.append(f"\n## Undeclared in catalog ({len(self.undeclared)})")
            parts.append("These dependencies exist in code but are not declared:")
            for e in self.undeclared:
                parts.append(f"  - {e.dependency}")

        if self.missing_in_code:
            parts.append(f"\n## Missing in code ({len(self.missing_in_code)})")
            parts.append("These are declared but no code evidence was found:")
            for e in self.missing_in_code:
                parts.append(f"  - {e.dependency}")

        if not self.confirmed and not self.undeclared and not self.missing_in_code:
            parts.append("No dependencies found.")

        return "\n".join(parts)
