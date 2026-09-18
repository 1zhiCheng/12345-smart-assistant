"""Redis Stream worker：处理异步文档入库、Loop 唤醒等作业。"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path

from app.config import get_settings
from app.deps import build_container
from app.utils.logging import setup_logging


async def process(container, job):
    payload = job.get("payload") or {}
    if job["type"] == "ingest_document":
        path = Path(payload["path"])
        try:
            doc = await container.indexer.ingest(
                path, payload["dept_id"], payload["uploaded_by"]
            )
            # 持久文件使用 UUID 命名，恢复原始文件名用于展示和版本判断。
            doc.setdefault("source", {})["file_name"] = payload.get("original_name", path.name)
            await container.store.update_document(doc["_id"], {"source": doc["source"]})
            relations = await container.conflict_detector.run_for_document(doc)
            review = await container.review_engine.create_review_order(doc)
            return {"document_id": doc["_id"], "relations": len(relations), "review_id": (review or {}).get("_id")}
        finally:
            path.unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass
    if job["type"] in {"feedback_received", "run_loop"}:
        async def progress(stage, detail):
            await container.job_queue.update_progress(job["_id"], {
                "stage": stage, "detail": detail,
            })

        result = await container.loop_engine.run_cycle(progress_callback=progress)
        result["memory_retention"] = await container.memory_retention.prune_expired()
        return result
    if job["type"] == "health_probe":
        return {"nonce": payload.get("nonce"), "worker": container.job_queue.consumer_name}
    raise ValueError(f"未知作业类型: {job['type']}")


async def main():
    settings = get_settings()
    setup_logging(settings.log_level)
    container = build_container(settings)
    if container.mongo is not None:
        await container.mongo.connect()
    if hasattr(container.session_store, "connect"):
        await container.session_store.connect()
    if settings.storage_mode == "mongo":
        await container.embeddings.embed(["芜湖市12345异步任务真实向量健康检查"])
        if container.embeddings.last_effective_provider in {None, "hash", "hash-fallback"}:
            raise RuntimeError("worker 未使用真实 embedding provider")
    async def keep_heartbeat() -> None:
        while True:
            await container.job_queue.heartbeat(settings.async_worker_stale_seconds)
            await asyncio.sleep(settings.async_worker_heartbeat_seconds)

    heartbeat_task = asyncio.create_task(keep_heartbeat())
    try:
        while True:
            for job in await container.job_queue.next_jobs():
                try:
                    result = await process(container, job)
                    await container.job_queue.finish(job, "completed", result)
                except Exception as exc:  # noqa: BLE001 - 失败交给队列重试/死信策略
                    await container.job_queue.fail(job, str(exc))
            await asyncio.sleep(0.1)
    finally:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
        if container.mongo is not None:
            await container.mongo.close()
        if hasattr(container.session_store, "close"):
            await container.session_store.close()


if __name__ == "__main__":
    asyncio.run(main())
