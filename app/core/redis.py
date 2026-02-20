"""
Redis client factory.
Uses redis[hiredis] — the C-extension parser for 10x throughput vs pure Python.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

import redis.asyncio as aioredis

from app.core.config import settings


_redis_pool: aioredis.Redis | None = None


async def get_redis_pool() -> aioredis.Redis:
    """
    Return the shared Redis connection pool.
    Created once at application startup via lifespan handler.
    """
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=50,
        )
    return _redis_pool


async def close_redis_pool() -> None:
    """Called at application shutdown."""
    global _redis_pool
    if _redis_pool is not None:
        await _redis_pool.aclose()
        _redis_pool = None


async def get_redis() -> AsyncGenerator[aioredis.Redis, None]:
    """
    FastAPI dependency for Redis client.
    Usage: redis = Depends(get_redis)
    """
    pool = await get_redis_pool()
    yield pool