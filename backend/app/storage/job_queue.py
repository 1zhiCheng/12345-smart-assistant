"""Redis Stream 异步作业队列，带 Mongo/Memory 持久状态回退。"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any

from app.storage.store import DataStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobQueue:
    group_name = "workers"

    def __init__(
        self,
        store: DataStore,
        session_store,
        stream_name: str,
        max_attempts: int = 3,
        reclaim_idle_ms: int = 60_000,
    ) -> None:
        self.store = store
        self.session_store = session_store
        self.stream_name = stream_name
        self.dead_letter_stream = f"{stream_name}:dead"
        self.max_attempts = max(1, max_attempts)
        self.reclaim_idle_ms = max(1_000, reclaim_idle_ms)
        self.consumer_name = f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"

    async def enqueue(self, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        job = {
            "_id": "job_" + uuid.uuid4().hex, "type": job_type, "payload": payload,
            "status": "queued",
            "attempts": 0,
            "max_attempts": self.max_attempts,
            "created_at": _now(),
            "updated_at": _now(),
        }
        await self.store.upsert("async_jobs", job)
        redis = getattr(self.session_store, "_redis", None)
        if redis is not None:
            await redis.xadd(self.stream_name, {"job_id": job["_id"], "type": job_type})
        return job

    async def next_jobs(self, count: int = 10, block_ms: int = 1000) -> list[dict[str, Any]]:
        redis = getattr(self.session_store, "_redis", None)
        if redis is not None:
            try:
                await redis.xgroup_create(self.stream_name, self.group_name, id="0", mkstream=True)
            except Exception as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
            jobs: list[dict[str, Any]] = []

            # Worker 崩溃后，超过可见性超时的 pending 消息由新 worker 接管。
            claimed = await redis.xautoclaim(
                self.stream_name,
                self.group_name,
                self.consumer_name,
                min_idle_time=self.reclaim_idle_ms,
                start_id="0-0",
                count=count,
            )
            claimed_entries = claimed[1] if claimed and len(claimed) > 1 else []
            jobs.extend(await self._load_entries(redis, claimed_entries, reclaimed=True))

            remaining = max(0, count - len(jobs))
            if remaining:
                records = await redis.xreadgroup(
                    self.group_name,
                    self.consumer_name,
                    {self.stream_name: ">"},
                    count=remaining,
                    block=block_ms,
                )
                for _, entries in records:
                    jobs.extend(await self._load_entries(redis, entries, reclaimed=False))
            return jobs
        jobs = await self.store.find("async_jobs", {"status": "queued"}, limit=count)
        for job in jobs:
            self._mark_running(job, reclaimed=False)
            await self.store.upsert("async_jobs", job)
        return jobs

    async def _load_entries(self, redis, entries, reclaimed: bool) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        for stream_id, fields in entries:
            job_id = fields.get("job_id") or fields.get(b"job_id")
            if isinstance(job_id, bytes):
                job_id = job_id.decode("utf-8")
            job = await self.store.get("async_jobs", str(job_id)) if job_id else None
            # 已完成/不存在的重复消息直接确认，避免重复执行副作用。
            if not job or job.get("status") in {"completed", "dead_letter"}:
                await redis.xack(self.stream_name, self.group_name, stream_id)
                continue
            if not reclaimed and job.get("status") != "queued":
                await redis.xack(self.stream_name, self.group_name, stream_id)
                continue
            job["_stream_id"] = stream_id
            self._mark_running(job, reclaimed=reclaimed)
            await self.store.upsert(
                "async_jobs", {key: value for key, value in job.items() if key != "_stream_id"}
            )
            jobs.append(job)
        return jobs

    @staticmethod
    def _mark_running(job: dict[str, Any], reclaimed: bool) -> None:
        job["status"] = "running"
        job["attempts"] = int(job.get("attempts", 0)) + 1
        job["updated_at"] = _now()
        if reclaimed:
            job["reclaimed_at"] = job["updated_at"]

    async def finish(self, job: dict[str, Any], status: str, result: Any = None) -> None:
        stored = await self.store.get("async_jobs", job["_id"]) or job
        stored.update({"status": status, "result": result, "updated_at": _now()})
        await self.store.upsert("async_jobs", stored)
        redis = getattr(self.session_store, "_redis", None)
        if redis is not None and job.get("_stream_id"):
            await redis.xack(self.stream_name, self.group_name, job["_stream_id"])

    async def fail(self, job: dict[str, Any], error: str) -> str:
        """失败时重试；达到上限后写入死信流。返回 retrying/dead_letter。"""
        stored = await self.store.get("async_jobs", job["_id"]) or job
        attempts = int(stored.get("attempts", job.get("attempts", 1)))
        errors = list(stored.get("errors") or [])
        errors.append({"attempt": attempts, "error": str(error)[:2000], "at": _now()})
        stored["errors"] = errors[-self.max_attempts :]
        stored["updated_at"] = _now()
        redis = getattr(self.session_store, "_redis", None)

        if attempts < int(stored.get("max_attempts", self.max_attempts)):
            stored["status"] = "queued"
            await self.store.upsert("async_jobs", stored)
            if redis is not None:
                await redis.xadd(self.stream_name, {"job_id": stored["_id"], "type": stored["type"]})
                if job.get("_stream_id"):
                    await redis.xack(self.stream_name, self.group_name, job["_stream_id"])
            return "retrying"

        stored["status"] = "dead_letter"
        stored["result"] = {"error": str(error)[:2000]}
        stored["failed_at"] = _now()
        await self.store.upsert("async_jobs", stored)
        if redis is not None:
            await redis.xadd(
                self.dead_letter_stream,
                {"job_id": stored["_id"], "type": stored["type"], "error": str(error)[:2000]},
            )
            if job.get("_stream_id"):
                await redis.xack(self.stream_name, self.group_name, job["_stream_id"])
        return "dead_letter"

    async def update_progress(self, job_id: str, progress: dict[str, Any]) -> None:
        stored = await self.store.get("async_jobs", job_id)
        if not stored:
            return
        stored.update({"status": "running", "progress": progress, "updated_at": _now()})
        await self.store.upsert("async_jobs", stored)

    async def heartbeat(self, ttl_seconds: int = 30) -> None:
        redis = getattr(self.session_store, "_redis", None)
        if redis is not None:
            await redis.set(
                f"{self.stream_name}:worker:heartbeat",
                self.consumer_name,
                ex=max(2, ttl_seconds),
            )

    async def worker_is_alive(self) -> bool:
        redis = getattr(self.session_store, "_redis", None)
        if redis is None:
            return False
        return bool(await redis.exists(f"{self.stream_name}:worker:heartbeat"))
