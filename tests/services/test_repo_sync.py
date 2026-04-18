from __future__ import annotations

import hmac
from textwrap import dedent
from hashlib import sha256
from pathlib import Path

import pytest

from mimir.domain.config import AdminConfig, MimirConfig, RepoConfig
from mimir.services.repo_sync import RepoSyncQueue, RepoSyncService, parse_webhook_payload


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


@pytest.mark.asyncio
async def test_repo_sync_queue_deduplicates_running_or_queued_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMIR_WEBHOOK_SECRET", "topsecret")
    svc = RepoSyncService(_config(tmp_path))
    monkeypatch.setattr(
        svc,
        "sync_repo",
        lambda repo_name, commit_sha=None: _sync_result(repo_name, commit_sha),
    )

    seen: list[str] = []

    async def runner(repo_name: str) -> dict:
        seen.append(repo_name)
        return {"repo": repo_name}

    queue = RepoSyncQueue(svc, runner)
    queue.start()
    try:
        job1 = queue.enqueue("payments", commit_sha="abc123")
        job2 = queue.enqueue("payments", commit_sha="def456")

        assert job1.id == job2.id
        assert job2.commit_sha == "def456"

        await queue._queue.join()
        assert seen == ["payments"]
        assert queue.get(job1.id).status == "completed"
    finally:
        await queue.stop()


def test_repo_sync_queue_keeps_follow_up_job_when_previous_is_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMIR_WEBHOOK_SECRET", "topsecret")
    svc = RepoSyncService(_config(tmp_path))
    queue = RepoSyncQueue(svc, lambda repo_name: _sync_result(repo_name, None))

    job1 = queue.enqueue("payments", commit_sha="abc123")
    job1.status = "running"
    job2 = queue.enqueue("payments", commit_sha="def456")

    assert job1.id != job2.id
    assert job2.commit_sha == "def456"


def test_repo_sync_queue_prunes_old_terminal_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMIR_WEBHOOK_SECRET", "topsecret")
    svc = RepoSyncService(_config(tmp_path))
    queue = RepoSyncQueue(svc, lambda repo_name: _sync_result(repo_name, None), history_limit=1)

    job1 = queue.enqueue("payments", commit_sha="abc123")
    job1.status = "completed"
    job2 = queue.enqueue("payments", commit_sha="def456")
    job2.status = "failed"
    job3 = queue.enqueue("payments", commit_sha="ghi789")

    assert queue.get(job1.id) is None
    assert queue.get(job2.id) is job2
    assert queue.get(job3.id) is job3


async def _sync_result(repo_name: str, commit_sha: str | None) -> dict:
    return {"repo": repo_name, "commit": commit_sha}
