"""桌面 memory 模式也必须实际消费异步作业，不能让管理端永久停在 queued。"""
from __future__ import annotations

import pytest

from app.async_jobs.local_worker import run_pending_jobs_once


@pytest.mark.asyncio
async def test_desktop_worker_consumes_loop_job(fresh_container):
    container = fresh_container
    job = await container.job_queue.enqueue("run_loop", {"requested_by": "admin"})

    consumed = await run_pending_jobs_once(container)

    saved = await container.store.get("async_jobs", job["_id"])
    assert consumed == 1
    assert saved is not None
    assert saved["status"] == "completed"
    assert "observed" in saved["result"]
