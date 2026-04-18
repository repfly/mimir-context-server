"""Admin endpoint authentication helpers."""

from __future__ import annotations

import hmac
import os

from aiohttp import web

from mimir.domain.config import MimirConfig


def require_admin_token(request: web.Request, config: MimirConfig) -> None:
    """Enforce bearer-token auth for admin-only HTTP endpoints."""
    expected = _resolve_admin_token(config)
    if not expected:
        raise web.HTTPServiceUnavailable(
            text='{"error":"Admin authentication is not configured"}',
            content_type="application/json",
        )

    provided = _extract_token(request)
    if not provided or not hmac.compare_digest(provided, expected):
        raise web.HTTPUnauthorized(
            text='{"error":"Invalid admin token"}',
            content_type="application/json",
        )


def _resolve_admin_token(config: MimirConfig) -> str | None:
    for env_name in (config.admin.admin_token_env, config.admin.webhook_secret_env):
        if not env_name:
            continue
        value = os.environ.get(env_name)
        if value:
            return value
    return None


def _extract_token(request: web.Request) -> str | None:
    header = request.headers.get("Authorization", "").strip()
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        return token or None
    token = request.headers.get("X-Mimir-Admin-Token", "").strip()
    return token or None
