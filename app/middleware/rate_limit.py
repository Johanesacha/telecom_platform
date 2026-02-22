"""
Rate Limit Middleware — sliding window rate limiter using Redis sorted sets.

Algorithm: Sliding Window with Redis Sorted Set
  Key:    ratelimit:{sha256(bearer_token)[:32]}
  Member: UUID4 per request (unique, prevents ZADD collision)
  Score:  Unix timestamp in milliseconds

  Per request:
    1. ZREMRANGEBYSCORE — remove members older than (now - window)
    2. ZCARD — count current members in window
    3. If count >= limit → 429 with headers
    4. ZADD now member — record this request
    5. PEXPIRE — ensure key expires after window

  Entire operation runs as a single Lua script — atomic, no race window.

Why sliding window vs token bucket:
  Token bucket: burst at T=0:00 and T=0:59 can both be within quota
  if the bucket refilled. Permits double-burst at window boundaries.
  Sliding window: at any moment, only the last 60 seconds count.
  A burst that exhausts quota at T=0:00 is still blocking at T=0:59.
  Stricter, fairer, and the correct choice for API rate limiting.

Bearer token extraction:
  Rate limiting happens BEFORE call_next(). The auth dependency runs
  INSIDE call_next() — so request.state.api_key is NOT available here.
  We extract the raw bearer token from the Authorization header and
  use its SHA-256 hash as the bucket key. The token is never stored.

  Key type detection: if 'sandbox' appears in the token string,
  use the sandbox burst limit. Otherwise use the free plan limit.
  The auth dependency will reject genuinely invalid keys with 401 —
  rate limiting a request that would have been rejected anyway is harmless.

Fail-open on Redis unavailability:
  If Redis is unreachable, rate limiting is skipped and the request
  proceeds normally. An unavailable rate limiter should not take down
  the API — the Redis health check in /health will surface the problem.

Exempt paths:
  /health, /docs, /openapi.json, /redoc — never rate limited.
  These must remain accessible even during DDoS or misconfiguration.
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid as uuid_mod

import redis.asyncio as aioredis
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.core.config import settings

logger = logging.getLogger(__name__)

# Sliding window duration in seconds
_WINDOW_SECONDS: int = 60

# Paths that bypass rate limiting entirely
_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
)

# Lua script: atomic sliding window check-and-record
# Returns: {allowed (0/1), current_count, reset_epoch_ms}
_LUA_SLIDING_WINDOW: str = """
local key        = KEYS[1]
local now_ms     = tonumber(ARGV[1])
local window_ms  = tonumber(ARGV[2])
local limit      = tonumber(ARGV[3])
local member     = ARGV[4]

-- Step 1: Evict expired members (older than window)
redis.call('ZREMRANGEBYSCORE', key, '-inf', now_ms - window_ms)

-- Step 2: Count members currently in window
local count = tonumber(redis.call('ZCARD', key))

-- Step 3: Reject if at or over limit
if count >= limit then
    -- Find when the oldest in-window entry will expire
    -- so we can tell the client exactly when to retry
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local reset_ms = now_ms + window_ms
    if #oldest >= 2 then
        reset_ms = tonumber(oldest[2]) + window_ms
    end
    return {0, count, reset_ms}
end

-- Step 4: Admit — record this request with current timestamp as score
redis.call('ZADD', key, now_ms, member)

-- Step 5: Expire the key slightly after the window to clean up Redis memory
redis.call('PEXPIRE', key, window_ms + 1000)

-- Return: admitted=1, new count, reset time
return {1, count + 1, now_ms + window_ms}
"""


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Sliding window rate limiter.

    Registration in main.py (innermost — added first, executes last in pre-processing):
        app.add_middleware(RateLimitMiddleware)  # first added → innermost
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        # Lazy-initialised on first request — avoids creating connections
        # at import time before the event loop is running.
        self._redis: aioredis.Redis | None = None

    async def _get_redis(self) -> aioredis.Redis:
        """
        Return the shared Redis client, initialising on first call.

        decode_responses=False because the Lua script returns integer arrays —
        aioredis returns them as Python ints when responses are raw bytes.
        """
        if self._redis is None:
            self._redis = aioredis.from_url(
                str(settings.redis_url),
                encoding="utf-8",
                decode_responses=False,
            )
        return self._redis

    @staticmethod
    def _is_exempt(path: str) -> bool:
        """Return True for paths that bypass rate limiting."""
        for prefix in _EXEMPT_PREFIXES:
            if path == prefix or path.startswith(prefix + "/"):
                return True
        return False

    @staticmethod
    def _extract_bearer_token(request: Request) -> str | None:
        """
        Extract the raw bearer token from the Authorization header.

        Returns None if no valid Bearer token is present.
        Does NOT validate the token — that is the auth dependency's job.
        """
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return None
        token = auth_header[7:].strip()
        return token if token else None

    @staticmethod
    def _get_limit(token: str) -> int:
        """
        Determine the burst rate limit for this token.

        Detection order:
          1. Token contains 'sandbox' → sandbox limit (more generous
             because sandbox developers need to run automated tests)
          2. Default → free plan limit (conservative, safe)

        The auth dependency enforces actual plan entitlements.
        This limit is for abuse prevention at the network level,
        not for quota enforcement (that is QuotaService's job).
        """
        token_lower = token.lower()
        if "sandbox" in token_lower:
            return int(settings.rate_limit_burst_sandbox)
        # Default to FREE — conservative. Invalid keys hit 401 from auth
        # before consuming meaningful resources behind the rate limit.
        return int(settings.rate_limit_burst_free)

    @staticmethod
    def _make_bucket_key(token: str) -> str:
        """
        Derive the Redis key for this token's rate limit bucket.

        SHA-256 hash truncated to 32 hex chars (128 bits of entropy).
        The raw token is never written to Redis — only its hash.
        Collision probability over millions of keys is negligible.
        """
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return f"ratelimit:{digest[:32]}"

    @staticmethod
    def _build_429_response(
        limit: int,
        reset_epoch_s: int,
    ) -> JSONResponse:
        """Build a well-formed 429 response with RFC-standard headers."""
        retry_after = max(1, reset_epoch_s - int(time.time()))
        return JSONResponse(
            status_code=429,
            content={
                "success": False,
                "data": None,
                "error": {
                    "code": "RATE_001",
                    "message": (
                        f"Rate limit exceeded. "
                        f"Maximum {limit} requests per {_WINDOW_SECONDS} seconds. "
                        f"Retry after {retry_after} seconds."
                    ),
                    "field": None,
                },
                "meta": None,  # filled by error handler in main.py if needed
            },
            headers={
                "Retry-After": str(retry_after),
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(reset_epoch_s),
                "Content-Type": "application/json",
            },
        )

    async def dispatch(self, request: Request, call_next) -> Response:
        # ── Exempt paths always pass through ──────────────────────────────
        if self._is_exempt(request.url.path):
            return await call_next(request)

        # ── Extract token — no token means skip rate limiting ─────────────
        # Unauthenticated requests will be rejected by the auth dependency.
        # Rate limiting without a token would block all public endpoints.
        token = self._extract_bearer_token(request)
        if token is None:
            return await call_next(request)

        limit = self._get_limit(token)
        bucket_key = self._make_bucket_key(token)

        now_ms = int(time.time() * 1000)
        window_ms = _WINDOW_SECONDS * 1000
        # Unique member per request prevents ZADD from deduplicating
        # simultaneous requests with the same millisecond timestamp
        member = str(uuid_mod.uuid4())

        # ── Sliding window check ───────────────────────────────────────────
        try:
            redis_client = await self._get_redis()
            result = await redis_client.eval(
                _LUA_SLIDING_WINDOW,
                1,           # numkeys
                bucket_key,  # KEYS[1]
                now_ms,      # ARGV[1]
                window_ms,   # ARGV[2]
                limit,       # ARGV[3]
                member,      # ARGV[4]
            )
            # Lua returns: {allowed(0|1), count, reset_ms}
            allowed: int = int(result[0])
            count: int = int(result[1])
            reset_ms: int = int(result[2])

        except Exception:
            # Fail open — Redis unavailable should not take down the API.
            # The /health endpoint will surface the Redis problem.
            logger.warning(
                "RateLimitMiddleware: Redis unavailable — failing open",
                extra={"path": request.url.path},
            )
            return await call_next(request)

        reset_epoch_s = reset_ms // 1000
        remaining = max(0, limit - count)

        # ── Reject over-limit requests ─────────────────────────────────────
        if not allowed:
            logger.info(
                "RateLimitMiddleware: request rejected — 429",
                extra={
                    "bucket_key": bucket_key,
                    "count": count,
                    "limit": limit,
                    "reset_epoch_s": reset_epoch_s,
                },
            )
            return self._build_429_response(limit, reset_epoch_s)

        # ── Admit request ─────────────────────────────────────────────────
        response = await call_next(request)

        # Attach rate limit headers to every admitted response.
        # Clients use these to implement client-side throttling proactively.
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset_epoch_s)

        return response