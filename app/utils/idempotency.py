"""
Idempotency key helpers for SMS and payment services.

Enforces the correct check-before-create ordering.
The cache check MUST happen before the database write — never after.

Cache key format:
    idempotency:{app_id}:{service}:{idempotency_key}

TTL: 24 hours. After 24 hours, the same idempotency key may be reused
for a new request — matching standard API idempotency conventions
(Stripe, Twilio both use 24-hour windows).
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from uuid import UUID

import redis.asyncio as aioredis

T = TypeVar("T")

# 24 hours in seconds — idempotency window
_IDEMPOTENCY_TTL_SECONDS: int = 86_400
_KEY_PREFIX: str = "idempotency"


def build_cache_key(
    app_id: UUID,
    service: str,
    idempotency_key: str,
) -> str:
    """
    Build a Redis cache key for an idempotency entry.

    Format: idempotency:{app_id}:{service}:{idempotency_key}

    The app_id scope is mandatory — the same idempotency_key string
    from two different applications must resolve independently.
    The service segment prevents cross-service key collisions.
    """
    return f"{_KEY_PREFIX}:{app_id}:{service}:{idempotency_key}"


async def get_cached_response(
    redis: aioredis.Redis,
    app_id: UUID,
    service: str,
    idempotency_key: str,
) -> dict | None:
    """
    Retrieve a cached idempotency response if it exists.

    Returns the deserialised response dict if the key is in cache.
    Returns None if this is a genuinely new request.

    Call this FIRST, before any business logic or database writes.
    If this returns a non-None value, return it immediately as HTTP 200.
    """
    cache_key = build_cache_key(app_id, service, idempotency_key)
    cached = await redis.get(cache_key)

    if cached is None:
        return None

    try:
        return json.loads(cached)
    except json.JSONDecodeError:
        # Corrupted cache entry — treat as cache miss.
        # Do not raise: a corrupted entry should not block a valid request.
        # The create path will overwrite the corrupted entry.
        return None


async def cache_response(
    redis: aioredis.Redis,
    app_id: UUID,
    service: str,
    idempotency_key: str,
    response_data: dict,
) -> None:
    """
    Store a response in the idempotency cache.

    Call this AFTER a successful database write, never before.
    The correct ordering is:

        1. get_cached_response()  → None means new request
        2. [business logic]
        3. [database write]
        4. cache_response()       → cache the result

    If the application crashes between step 3 and step 4, the next
    retry will miss the cache, reach the database write, and receive
    an IntegrityError (unique constraint on idempotency_key column).
    The service layer catches IntegrityError and re-fetches the record.
    This recovers correctly without double-processing.

    TTL is 24 hours. After expiry, the same key may be reused.
    """
    cache_key = build_cache_key(app_id, service, idempotency_key)
    await redis.setex(
        cache_key,
        _IDEMPOTENCY_TTL_SECONDS,
        json.dumps(response_data, default=str),
    )


async def get_or_create(
    *,
    redis: aioredis.Redis,
    app_id: UUID,
    service: str,
    idempotency_key: str | None,
    create_fn: Callable[[], Awaitable[T]],
    serialise_fn: Callable[[T], dict],
) -> tuple[T | dict, bool]:
    """
    Idempotent get-or-create operation.

    Encapsulates the complete idempotency flow in one call.
    The create_fn is called only on a genuine first request.
    The serialise_fn converts the created object to a cacheable dict.

    Args:
        redis:           Redis client.
        app_id:          Application UUID for cache key scoping.
        service:         Service name string ('sms', 'payments', etc.).
        idempotency_key: Key from client request header. If None,
                         no caching is performed — create_fn always runs.
        create_fn:       Async callable that performs the database write
                         and returns the created domain object.
        serialise_fn:    Converts the created object to a JSON-safe dict
                         for cache storage.

    Returns:
        (result, is_cached) tuple.
        result:    The domain object (new creation) or cached dict (replay).
        is_cached: True if result came from cache (use HTTP 200 not 201).
                   False if result was freshly created (use HTTP 201).

    Example usage in SMSService:
        result, is_cached = await get_or_create(
            redis=redis,
            app_id=api_key.application_id,
            service='sms',
            idempotency_key=request.idempotency_key,
            create_fn=lambda: sms_repo.create(**fields),
            serialise_fn=lambda msg: {'id': str(msg.id), 'status': msg.status},
        )
        status_code = 200 if is_cached else 201
    """
    if idempotency_key is None:
        result = await create_fn()
        return result, False

    # Step 1 — cache check BEFORE any business logic
    cached = await get_cached_response(redis, app_id, service, idempotency_key)
    if cached is not None:
        return cached, True

    # Step 2 — genuine new request: create
    result = await create_fn()

    # Step 3 — cache the result for future retries
    await cache_response(
        redis, app_id, service, idempotency_key, serialise_fn(result)
    )

    return result, False