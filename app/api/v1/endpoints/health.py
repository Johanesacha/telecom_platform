"""
Health check endpoint — no authentication required.

Checks three components independently:
  database: SELECT 1 on the async session
  redis:    PING via the shared connection pool
  celery:   inspect.ping() run in a thread pool (blocking call, 2s timeout)

Returns 200 if all healthy, 503 if any component is degraded.
The request still receives a structured response on 503 — clients can
parse which component failed without checking the status code.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import redis.asyncio as aioredis

from app.api.deps import get_db, get_redis

router = APIRouter(tags=["Health"])


@router.get("/health", summary="Platform health check")
async def health_check(
    request: Request,
    session: AsyncSession = Depends(get_db),
    redis_client: aioredis.Redis = Depends(get_redis),
) -> JSONResponse:
    components: dict = {}
    overall = True

    # ── Database ───────────────────────────────────────────────────────────
    try:
        await session.execute(text("SELECT 1"))
        components["database"] = {"status": "healthy"}
    except Exception as exc:
        components["database"] = {"status": "degraded", "error": str(exc)[:120]}
        overall = False

    # ── Redis ──────────────────────────────────────────────────────────────
    try:
        await redis_client.ping()
        components["redis"] = {"status": "healthy"}
    except Exception as exc:
        components["redis"] = {"status": "degraded", "error": str(exc)[:120]}
        overall = False

    # ── Celery ─────────────────────────────────────────────────────────────
    # inspect.ping() is a blocking call — must run in thread pool to avoid
    # blocking the asyncio event loop during the 2-second timeout window.
    try:
        from app.core.celery_app import celery_app

        inspector = celery_app.control.inspect(timeout=2.0)
        ping_result = await asyncio.to_thread(inspector.ping)
        if ping_result:
            components["celery"] = {
                "status": "healthy",
                "workers": len(ping_result),
            }
        else:
            components["celery"] = {
                "status": "degraded",
                "error": "No workers responded within 2 seconds",
            }
            overall = False
    except Exception as exc:
        components["celery"] = {"status": "degraded", "error": str(exc)[:120]}
        overall = False

    return JSONResponse(
        status_code=200 if overall else 503,
        content={
            "status": "healthy" if overall else "degraded",
            "components": components,
            "request_id": getattr(request.state, "request_id", "unknown"),
        },
    )