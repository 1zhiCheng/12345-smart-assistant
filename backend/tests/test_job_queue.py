"""异步队列的持久作业状态测试。"""
from __future__ import annotations

import pytest

from app.storage.job_queue import JobQueue
from app.storage.redis_store import MemorySessionStore
from app.storage.store import MemoryStore


@pytest.mark.asyncio
async def test_job_queue_memory_fallback():
    store = MemoryStore()
    queue = JobQueue(store, MemorySessionStore(), "jobs")
    job = await queue.enqueue("run_loop", {"requested_by": "admin"})
    pending = await queue.next_jobs()
    assert pending[0]["_id"] == job["_id"]
    assert pending[0]["status"] == "running"
    assert (await store.get("async_jobs", job["_id"]))["status"] == "running"
    await queue.update_progress(job["_id"], {"stage": "reflect", "detail": "归因中"})
    progressing = await store.get("async_jobs", job["_id"])
    assert progressing["progress"]["stage"] == "reflect"
    await queue.finish(job, "completed", {"observed": 1})
    saved = await store.get("async_jobs", job["_id"])
    assert saved["status"] == "completed"


@pytest.mark.asyncio
async def test_job_queue_retries_then_moves_to_dead_letter_in_memory():
    store = MemoryStore()
    queue = JobQueue(store, MemorySessionStore(), "jobs", max_attempts=2)
    job = await queue.enqueue("bad_job", {})

    first = (await queue.next_jobs())[0]
    assert first["attempts"] == 1
    assert await queue.fail(first, "first failure") == "retrying"
    assert (await store.get("async_jobs", job["_id"]))["status"] == "queued"

    second = (await queue.next_jobs())[0]
    assert second["attempts"] == 2
    assert await queue.fail(second, "second failure") == "dead_letter"
    saved = await store.get("async_jobs", job["_id"])
    assert saved["status"] == "dead_letter"
    assert len(saved["errors"]) == 2
    assert saved["result"]["error"] == "second failure"


class _FakeRedis:
    def __init__(self):
        self.entries = []
        self.pending = []
        self.dead = []
        self.values = {}
        self.sequence = 0

    async def xadd(self, stream, fields):
        self.sequence += 1
        entry = (f"{self.sequence}-0", dict(fields))
        if stream.endswith(":dead"):
            self.dead.append(entry)
        else:
            self.entries.append(entry)
        return entry[0]

    async def xgroup_create(self, *_args, **_kwargs):
        return True

    async def xreadgroup(self, *_args, **_kwargs):
        if not self.entries:
            return []
        entries, self.entries = self.entries, []
        self.pending.extend(entries)
        return [("jobs", entries)]

    async def xautoclaim(self, *_args, **_kwargs):
        return ["0-0", list(self.pending), []]

    async def xack(self, _stream, _group, stream_id):
        self.pending = [entry for entry in self.pending if entry[0] != stream_id]
        return 1

    async def set(self, key, value, ex=None):
        self.values[key] = (value, ex)

    async def exists(self, key):
        return int(key in self.values)


class _RedisSession:
    def __init__(self, redis):
        self._redis = redis


@pytest.mark.asyncio
async def test_job_queue_reclaims_stale_redis_pending_job():
    store = MemoryStore()
    redis = _FakeRedis()
    first_queue = JobQueue(store, _RedisSession(redis), "jobs", reclaim_idle_ms=1000)
    job = await first_queue.enqueue("run_loop", {})
    running = (await first_queue.next_jobs())[0]
    assert running["attempts"] == 1

    second_queue = JobQueue(store, _RedisSession(redis), "jobs", reclaim_idle_ms=1000)
    reclaimed = (await second_queue.next_jobs())[0]
    assert reclaimed["_id"] == job["_id"]
    assert reclaimed["attempts"] == 2
    assert "reclaimed_at" in reclaimed


@pytest.mark.asyncio
async def test_job_queue_redis_dead_letter_and_worker_heartbeat():
    store = MemoryStore()
    redis = _FakeRedis()
    queue = JobQueue(store, _RedisSession(redis), "jobs", max_attempts=1)
    await queue.heartbeat(ttl_seconds=15)
    assert await queue.worker_is_alive()

    await queue.enqueue("bad_job", {})
    running = (await queue.next_jobs())[0]
    assert await queue.fail(running, "fatal") == "dead_letter"
    assert len(redis.dead) == 1
