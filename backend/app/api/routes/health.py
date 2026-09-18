"""健康检查 / 就绪探针。"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request):
    container = request.app.state.container
    issues = []
    details = {"storage_mode": container.settings.storage_mode}
    if container.settings.storage_mode == "mongo":
        mongo_ok = await container.mongo.ping()
        redis_ok = await container.session_store.ping()
        worker_ok = await container.job_queue.worker_is_alive()
        vector_ok = container.embeddings.last_effective_provider not in {None, "hash", "hash-fallback"}
        details.update({
            "mongodb": mongo_ok,
            "redis": redis_ok,
            "worker": worker_ok,
            "embedding_provider": container.embeddings.last_effective_provider,
            "real_vectors": vector_ok,
        })
        if not mongo_ok:
            issues.append("mongodb")
        if not redis_ok:
            issues.append("redis")
        if container.settings.worker_readiness_required and not worker_ok:
            issues.append("worker")
        if not vector_ok:
            issues.append("real_vectors")
    if issues:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "issues": issues, "details": details},
        )
    return {"status": "ready", "details": details}
