from __future__ import annotations

import hmac
from textwrap import dedent
from hashlib import sha256
from pathlib import Path

import pytest

from mimir.domain.config import AdminConfig, MimirConfig, RepoConfig
from mimir.services.repo_sync import RepoSyncService, parse_webhook_payload


def _config(tmp_path: Path) -> MimirConfig:
    return MimirConfig(
        repos=[
            RepoConfig(
                name="payments",
                path=tmp_path / "repos" / "payments",
                clone_url="git@github.com:acme/payments.git",
                branch="main",
                webhook_repo="payments-service",
            ),
        ],
        data_dir=tmp_path / ".mimir",
        admin=AdminConfig(
            webhook_secret_env="MIMIR_WEBHOOK_SECRET",
            mirrors_dir=str(tmp_path / "mirrors"),
            enable_repo_sync=True,
        ),
    )


def test_config_allows_missing_repo_path_when_clone_url_is_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMIR_WEBHOOK_SECRET", "topsecret")
    cfg = _config(tmp_path)

    assert cfg.repos[0].clone_url == "git@github.com:acme/payments.git"
    assert cfg.repos[0].branch == "main"
    assert cfg.admin.enable_repo_sync is True


def test_config_load_preserves_repo_sync_fields(tmp_path: Path) -> None:
    config_path = tmp_path / "mimir.toml"
    config_path.write_text(dedent("""
        [admin]
        webhook_secret_env = "MIMIR_WEBHOOK_SECRET"
        admin_token_env = "MIMIR_ADMIN_TOKEN"
        enable_repo_sync = true
        job_history_limit = 17

        [[repos]]
        name = "payments"
        path = "repos/payments"
        clone_url = "git@github.com:acme/payments.git"
        branch = "release"
        webhook_repo = "payments-service"
    """))

    cfg = MimirConfig.load(config_path)

    assert cfg.repos[0].clone_url == "git@github.com:acme/payments.git"
    assert cfg.repos[0].branch == "release"
    assert cfg.repos[0].webhook_repo == "payments-service"
    assert cfg.admin.admin_token_env == "MIMIR_ADMIN_TOKEN"
    assert cfg.admin.job_history_limit == 17


def test_verify_signature_accepts_sha256_hex_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMIR_WEBHOOK_SECRET", "topsecret")
    svc = RepoSyncService(_config(tmp_path))
    body = b'{"repo":"payments-service","branch":"main","commit_sha":"abc123"}'
    digest = hmac.new(b"topsecret", body, sha256).hexdigest()

    assert svc.verify_signature(body, f"sha256={digest}") is True
    assert svc.verify_signature(body, digest) is True
    assert svc.verify_signature(body, "sha256=wrong") is False


def test_parse_webhook_payload_normalizes_branch() -> None:
    payload = parse_webhook_payload(
        b'{"repository":{"name":"payments-service"},"ref":"refs/heads/main","after":"abc123"}'
    )

    assert payload["repo"] == "payments-service"
    assert payload["branch"] == "main"
    assert payload["commit_sha"] == "abc123"
