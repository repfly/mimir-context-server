"""Central repo mirroring and webhook-driven sync helpers."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from hashlib import sha256
from pathlib import Path
from typing import Optional

from mimir.domain.config import MimirConfig, RepoConfig


class RepoSyncService:
    """Owns git mirror/worktree management for centrally indexed repos."""

    def __init__(self, config: MimirConfig) -> None:
        self._config = config
        self._repos_by_name = {repo.name: repo for repo in config.repos}
        self._repos_by_webhook = {
            (repo.webhook_repo or repo.name): repo for repo in config.repos
        }

    def is_enabled(self) -> bool:
        return self._config.admin.enable_repo_sync

    def resolve_webhook_repo(self, repo_name: str) -> RepoConfig:
        repo = self._repos_by_webhook.get(repo_name)
        if repo is None:
            raise ValueError(f"Unknown webhook repo: {repo_name}")
        return repo

    def verify_signature(self, body: bytes, signature_header: str | None) -> bool:
        secret = self._secret()
        if not secret:
            return False
        if not signature_header:
            return False
        provided = signature_header.strip()
        if "=" in provided:
            _, provided = provided.split("=", 1)
        expected = hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()
        return hmac.compare_digest(provided, expected)

    async def sync_repo(self, repo_name: str, *, commit_sha: Optional[str] = None) -> dict:
        repo = self._repos_by_name.get(repo_name)
        if repo is None:
            raise ValueError(f"Unknown repo: {repo_name}")
        if not repo.clone_url:
            raise ValueError(f"Repo '{repo_name}' is not configured for central sync")

        mirror_dir = self._mirror_dir(repo)
        worktree_dir = Path(repo.path)

        await asyncio.to_thread(self._ensure_checkout, repo, mirror_dir, worktree_dir, commit_sha)
        return {
            "repo": repo.name,
            "branch": repo.branch,
            "commit": self.current_commit(repo.name),
            "path": str(worktree_dir),
        }

    def current_commit(self, repo_name: str) -> Optional[str]:
        try:
            import git

            repo = git.Repo(str(self._repos_by_name[repo_name].path))
            return repo.head.commit.hexsha
        except Exception:
            return None

    def _secret(self) -> Optional[str]:
        env = self._config.admin.webhook_secret_env
        if not env:
            return None
        return os.environ.get(env)

    def _mirror_dir(self, repo: RepoConfig) -> Path:
        root = self._config.admin.mirrors_dir or str(self._config.session_dir / "mirrors")
        root_path = Path(root)
        root_path.mkdir(parents=True, exist_ok=True)
        return root_path / f"{repo.name}.git"

    def _ensure_checkout(
        self,
        repo: RepoConfig,
        mirror_dir: Path,
        worktree_dir: Path,
        commit_sha: Optional[str],
    ) -> None:
        import git

        if mirror_dir.exists():
            git.Repo(str(mirror_dir)).remote().fetch(prune=True)
            mirror = git.Repo(str(mirror_dir))
        else:
            mirror = git.Repo.clone_from(repo.clone_url, str(mirror_dir), mirror=True)

        target_ref = commit_sha or f"origin/{repo.branch}"
        if commit_sha:
            try:
                mirror.commit(commit_sha)
            except Exception as exc:
                raise ValueError(f"Commit {commit_sha} not found in mirror for {repo.name}") from exc

        worktree_dir.parent.mkdir(parents=True, exist_ok=True)
        if not worktree_dir.exists():
            checkout_repo = git.Repo.clone_from(str(mirror_dir), str(worktree_dir))
        else:
            checkout_repo = git.Repo(str(worktree_dir))

        checkout_repo.remote().fetch(prune=True)
        checkout_repo.git.checkout("-B", repo.branch, f"origin/{repo.branch}")
        checkout_repo.git.reset("--hard", target_ref)


def parse_webhook_payload(body: bytes) -> dict:
    payload = json.loads(body.decode("utf-8"))
    repository = payload.get("repository")
    repo_from_repository = None
    if isinstance(repository, dict):
        repo_from_repository = repository.get("name")
    elif isinstance(repository, str):
        repo_from_repository = repository
    repo = (
        payload.get("repo")
        or repo_from_repository
    )
    branch = payload.get("branch") or payload.get("ref")
    commit_sha = (
        payload.get("commit_sha")
        or payload.get("after")
        or payload.get("checkout_sha")
    )
    if isinstance(branch, str) and branch.startswith("refs/heads/"):
        branch = branch.removeprefix("refs/heads/")
    return {"repo": repo, "branch": branch, "commit_sha": commit_sha, "raw": payload}
