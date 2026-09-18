"""仅供 memory/桌面模式使用的本地异步作业消费者。"""
from __future__ import annotations

import asyncio
from typing import Any

from scripts.async_worker import process

from app.utils.logging import get_logger

logger = get_logger(__name__)


async def run_pending_jobs_once(container: Any, max_jobs: int = 8) -> int:
    """消费一批 memory 队列作业，复用生产 Worker 的同一处理逻辑。

    此函数不启动 Redis，也不会伪造 Worker 心跳；生产持久化栈仍必须由独立进程消费。
    """
    jobs = await container.job_queue.next_jobs(count=max_jobs, block_ms=0)
    for job in jobs:
        try:
            result = await process(container, job)
            await container.job_queue.finish(job, "completed", result)
        except Exception as exc:  # noqa: BLE001 - 队列实现负责重试/死信状态
            logger.exception("桌面本地作业失败: id=%s type=%s", job.get("_id"), job.get("type"))
            await container.job_queue.fail(job, str(exc))
    return len(jobs)


async def serve_memory_queue(container: Any, stop_event: asyncio.Event, poll_seconds: float = 0.25) -> None:
    """在 FastAPI 生命周期内消费内存队列，退出时由 stop_event/cancel 安全结束。"""
    while not stop_event.is_set():
        processed = await run_pending_jobs_once(container)
        if processed:
            # 防止连续积压任务占满事件循环，使 HTTP 状态轮询仍能及时响应。
            await asyncio.sleep(0)
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
        except asyncio.TimeoutError:
            pass
