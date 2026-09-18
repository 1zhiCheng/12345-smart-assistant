"""容器健康检查：确认 Redis 中存在未过期的 worker 心跳。"""
from __future__ import annotations

import asyncio

from app.config import get_settings


async def main() -> None:
    import redis.asyncio as aioredis

    settings = get_settings()
    client = aioredis.from_url(
        settings.redis_addr,
        db=settings.redis_db,
        decode_responses=True,
        socket_connect_timeout=settings.redis_socket_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
    )
    try:
        key = f"{settings.async_stream_name}:worker:heartbeat"
        if not await client.exists(key):
            raise SystemExit("worker heartbeat missing")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
