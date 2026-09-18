"""等待 MongoDB / Redis 就绪（Docker 启动用）。"""
from __future__ import annotations

import asyncio
import os
import sys

from app.config import get_settings


async def wait_mongo(uri: str, timeout: float = 60.0) -> bool:
    from motor.motor_asyncio import AsyncIOMotorClient

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=2000)
            await client.admin.command("ping")
            print("MongoDB ready")
            client.close()
            return True
        except Exception:
            await asyncio.sleep(2)
    print("MongoDB 未就绪")
    return False


async def wait_redis(addr: str, timeout: float = 30.0) -> bool:
    import redis.asyncio as aioredis

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            r = aioredis.from_url(addr)
            await r.ping()
            await r.aclose()
            print("Redis ready")
            return True
        except Exception:
            await asyncio.sleep(2)
    print("Redis 未就绪")
    return False


async def main() -> None:
    settings = get_settings()
    if settings.storage_mode == "mongo":
        mongo_ok, redis_ok = await asyncio.gather(
            wait_mongo(settings.mongodb_uri), wait_redis(settings.redis_addr)
        )
        if not (mongo_ok and redis_ok):
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
