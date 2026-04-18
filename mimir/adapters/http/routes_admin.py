"""Admin-only HTTP endpoints."""

from __future__ import annotations

from aiohttp import web

from mimir.adapters.http.admin_auth import require_admin_token
from mimir.adapters.http.state import HttpServerState
from mimir.services.repo_sync import RepoSyncQueue, parse_webhook_payload


def register_admin_routes(
    routes: web.RouteTableDef,
    state: HttpServerState,
    sync_queue: RepoSyncQueue | None,
) -> None:
    if sync_queue is None:
        return

    @routes.get("/api/v1/admin/repos")
    async def api_admin_repos(request: web.Request) -> web.Response:
        require_admin_token(request, state.container.config)
        return web.json_response({"repos": sync_queue.list_repo_states()})

    @routes.get("/api/v1/admin/jobs/{job_id}")
    async def api_admin_job(request: web.Request) -> web.Response:
        require_admin_token(request, state.container.config)
        job = sync_queue.get(request.match_info["job_id"])
        if job is None:
            return web.json_response({"error": "Job not found"}, status=404)
        return web.json_response({
            "id": job.id,
            "repo": job.repo,
            "commit_sha": job.commit_sha,
            "status": job.status,
            "error": job.error,
            "result": job.result,
        })

    @routes.post("/api/v1/admin/repos/{repo}/sync")
    async def api_admin_repo_sync(request: web.Request) -> web.Response:
        require_admin_token(request, state.container.config)
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            job = sync_queue.enqueue(request.match_info["repo"], commit_sha=body.get("commit_sha"))
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=404)
        return web.json_response({"job_id": job.id, "status": job.status}, status=202)

    @routes.post("/api/v1/admin/webhooks/git")
    async def api_admin_webhook(request: web.Request) -> web.Response:
        body = await request.read()
        signature = request.headers.get("X-Mimir-Signature") or request.headers.get("X-Hub-Signature-256")
        if not state.container.repo_sync.verify_signature(body, signature):
            return web.json_response({"error": "Invalid signature"}, status=401)

        try:
            payload = parse_webhook_payload(body)
        except Exception:
            return web.json_response({"error": "Invalid webhook payload"}, status=400)

        if not payload.get("repo"):
            return web.json_response({"error": "Missing repo in webhook payload"}, status=400)

        try:
            repo_config = state.container.repo_sync.resolve_webhook_repo(payload["repo"])
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=404)

        branch = payload.get("branch")
        if branch and branch != repo_config.branch:
            return web.json_response({
                "status": "ignored",
                "reason": f"branch '{branch}' does not match tracked branch '{repo_config.branch}'",
            })

        job = sync_queue.enqueue(repo_config.name, commit_sha=payload.get("commit_sha"))
        return web.json_response({"job_id": job.id, "repo": repo_config.name, "status": job.status}, status=202)
