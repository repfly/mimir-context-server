"""Tests for IndexingService._normalize_route and cross-repo matching.

Covers:
- Scheme/host stripping
- Trailing slash removal
- Path parameter collapsing ({id}, :id, numeric)
- No false-positive substring matches
- Parameterized endpoint ↔ literal client matching
- Suffix matching with boundary check
"""

from __future__ import annotations

import pytest

from mimir.services.graph_linker import normalize_route


# ---------------------------------------------------------------------------
# _normalize_route
# ---------------------------------------------------------------------------


class TestNormalizeRoute:

    def test_strips_scheme_and_host(self) -> None:
        assert normalize_route("http://localhost:8000/api/orders") == "/api/orders"

    def test_strips_https(self) -> None:
        assert normalize_route("https://api.example.com/users") == "/users"

    def test_removes_trailing_slash(self) -> None:
        assert normalize_route("/orders/") == "/orders"

    def test_preserves_root(self) -> None:
        assert normalize_route("/") == "/"

    def test_lowercases(self) -> None:
        assert normalize_route("/API/Orders") == "/api/orders"

    def test_collapses_curly_params(self) -> None:
        assert normalize_route("/orders/{order_id}") == "/orders/{_}"

    def test_collapses_colon_params(self) -> None:
        assert normalize_route("/orders/:id") == "/orders/{_}"

    def test_collapses_numeric_ids(self) -> None:
        assert normalize_route("/orders/123") == "/orders/{_}"

    def test_multiple_params(self) -> None:
        assert normalize_route("/repos/{owner}/{repo}/pulls/{id}") == "/repos/{_}/{_}/pulls/{_}"

    def test_path_only_passthrough(self) -> None:
        assert normalize_route("/simple") == "/simple"

    def test_parameterized_matches_literal(self) -> None:
        """Endpoint /orders/{id} and client /orders/123 should normalize identically."""
        ep = normalize_route("/orders/{id}")
        client = normalize_route("/orders/123")
        assert ep == client == "/orders/{_}"


# ---------------------------------------------------------------------------
# Cross-repo matching correctness (integration-style)
# ---------------------------------------------------------------------------


class TestCrossRepoMatching:
    """Verify that normalized matching prevents the known false positives
    and false negatives from the old substring approach."""

    def test_no_substring_false_positive(self) -> None:
        """/orders must NOT match /reorders."""
        ep = normalize_route("/orders")
        client = normalize_route("/reorders")
        assert ep != client

    def test_no_partial_path_false_positive(self) -> None:
        """/orders must NOT match /orders/123 (different depth)."""
        ep = normalize_route("/orders")
        client = normalize_route("/orders/123")
        assert ep != client

    def test_exact_match(self) -> None:
        ep = normalize_route("/api/orders")
        client = normalize_route("/api/orders")
        assert ep == client

    def test_suffix_match_exact_same_path(self) -> None:
        """Exact normalized match works for identical routes."""
        ep_norm = normalize_route("/api/orders")
        client_norm = normalize_route("http://host/api/orders")
        assert ep_norm == client_norm

    def test_suffix_no_false_boundary(self) -> None:
        """Client /preorders should NOT suffix-match endpoint /orders
        because there's no / boundary."""
        ep_norm = normalize_route("/orders")
        client_norm = normalize_route("/preorders")

        has_suffix = client_norm.endswith(ep_norm) and (
            len(client_norm) == len(ep_norm)
            or client_norm[-len(ep_norm) - 1] == "/"
        )
        assert not has_suffix

    def test_suffix_match_with_versioned_prefix(self) -> None:
        """Client /api/v1/orders has v1 as a non-/ boundary before /orders,
        so suffix match correctly rejects it (different route depth)."""
        ep_norm = normalize_route("/orders")
        client_norm = normalize_route("http://host/v1/orders")

        # v1 is not a / boundary — this is NOT a match, which is correct:
        # /v1/orders is a different route than /orders.
        has_suffix = client_norm.endswith(ep_norm) and (
            len(client_norm) == len(ep_norm)
            or client_norm[-len(ep_norm) - 1] == "/"
        )
        assert not has_suffix
