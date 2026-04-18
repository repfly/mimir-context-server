from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web

from mimir.adapters.http.admin_auth import require_admin_token


def _request(**headers):
    return SimpleNamespace(headers=headers)


def _config(*, admin_env: str | None = None, webhook_env: str | None = None):
    return SimpleNamespace(
        admin=SimpleNamespace(
            admin_token_env=admin_env,
            webhook_secret_env=webhook_env,
        ),
    )


def test_require_admin_token_accepts_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMIR_ADMIN_TOKEN", "secret")

    require_admin_token(
        _request(Authorization="Bearer secret"),
        _config(admin_env="MIMIR_ADMIN_TOKEN"),
    )


def test_require_admin_token_falls_back_to_webhook_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMIR_WEBHOOK_SECRET", "secret")

    require_admin_token(
        _request(**{"X-Mimir-Admin-Token": "secret"}),
        _config(webhook_env="MIMIR_WEBHOOK_SECRET"),
    )


def test_require_admin_token_rejects_missing_or_invalid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMIR_ADMIN_TOKEN", "secret")

    with pytest.raises(web.HTTPUnauthorized):
        require_admin_token(
            _request(Authorization="Bearer wrong"),
            _config(admin_env="MIMIR_ADMIN_TOKEN"),
        )


def test_require_admin_token_fails_closed_when_not_configured() -> None:
    with pytest.raises(web.HTTPServiceUnavailable):
        require_admin_token(_request(), _config())
