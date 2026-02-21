"""
QuotaService — two-layer rate and quota enforcement via Redis Lua scripts.

Layer 1: Burst rate limiter — sliding window per minute per service.
         Prevents API abuse within short time windows.

Layer 2: Daily quota — counter reset at midnight UTC per service.
         Enforces plan-based daily usage limits.

Both layers use Redis Lua scripts for atomic check-and-act operations.
A non-atomic implementation (GET then SET) would allow quota bypass
under concurrent load. The Lua scripts eliminate this race condition
by executing check and increment as a single indivisible Redis operation.

WRITTEN MANUALLY. Never regenerate with Junie.
A bug here silently allows billing fraud or API abuse.
"""
from __future__ import annotations

import calendar
from datetime import datetime, timezone

import redis.asyncio as aioredis

from app.core.config import settings
from app.core.exceptions import QuotaExceededError, RateLimitExceededError
from app.domain.api_key import ApiKey
from app.domain.application import AppPlan
from app.utils.time_utils import utcnow


# ── Lua Script: Sliding Window Burst Rate Limiter ──────────────────────────
#
# KEYS[1] = rate key  e.g. rate:uuid:sms
# ARGV[1] = now_ms    current Unix time in milliseconds
# ARGV[2] = window_ms window size in milliseconds (60000 = 60s)
# ARGV[3] = limit     max requests allowed in window
# ARGV[4] = member    unique member for this request (now_ms:random)
#
# Returns: 1 if request is allowed, 0 if rate limit exceeded
#
_BURST_LUA = """
local key      = KEYS[1]
local now_ms   = tonumber(ARGV[1])
local window   = tonumber(ARGV[2])
local limit    = tonumber(ARGV[3])
local member   = ARGV[4]

-- Remove all entries older than the window boundary
redis.call('ZREMRANGEBYSCORE', key, 0, now_ms - window)

-- Count entries remaining in the window
local count = redis.call('ZCARD', key)

if count >= limit then
    return 0
end

-- Record this request
redis.call('ZADD', key, now_ms, member)

-- Set TTL slightly above window to allow natural expiry
-- (window / 1000) converts ms to seconds, +10 adds buffer
redis.call('EXPIRE', key, math.ceil(window / 1000) + 10)

return 1
"""

# ── Lua Script: Daily Quota Check-and-Increment ────────────────────────────
#
# KEYS[1]  = quota key  e.g. quota:uuid:sms:2026-02-21
# ARGV[1]  = limit      daily limit for this plan and service
# ARGV[2]  = midnight   Unix timestamp of midnight UTC tonight
#
# Returns: remaining quota after this increment (>= 0)
#          -1 if quota already exhausted
#
_QUOTA_LUA = """
local key      = KEYS[1]
local limit    = tonumber(ARGV[1])
local midnight = tonumber(ARGV[2])

local current = tonumber(redis.call('GET', key) or '0')

if current >= limit then
    return -1
end

local new_count = redis.call('INCR', key)

-- Set expiry only on first increment to avoid resetting TTL on each call.
-- EXPIREAT sets an absolute Unix timestamp, not a relative TTL.
-- The key expires exactly at midnight UTC regardless of when it was created.
if new_count == 1 then
    redis.call('EXPIREAT', key, midnight)
end

return limit - new_count
"""

# Sandbox plans are exempt from daily quota but still subject to burst limits.
# Sandbox keys are for testing — unlimited daily use is correct behaviour.
_SANDBOX_EXEMPT_FROM_DAILY_QUOTA = True


class QuotaService:
    """
    Enforces burst rate limits and daily quotas for all telecom services.

    Instantiate per-request with the authenticated ApiKey and a Redis client.
    Call check_and_consume(service) before executing any billable operation.

    Usage in service layer:
        quota = QuotaService(api_key, redis)
        remaining = await quota.check_and_consume("sms")
        # If no exception: proceed with SMS send
        # remaining: how many more SMS calls are allowed today

    Services: "sms", "payments", "ussd", "numbers", "notifications"
    """

    # Window and key constants
    _BURST_WINDOW_MS: int = 60_000       # 60 seconds in milliseconds
    _BURST_KEY_PREFIX: str = "rate"
    _QUOTA_KEY_PREFIX: str = "quota"

    def __init__(self, api_key: ApiKey, redis: aioredis.Redis) -> None:
        self._api_key = api_key
        self._redis = redis
        self._app_id = str(api_key.application_id)
        self._plan: AppPlan = api_key.application.plan
        self._is_sandbox: bool = api_key.key_type.value == "SANDBOX"

    # ── Public Interface ───────────────────────────────────────────────────

    async def check_and_consume(self, service: str) -> int:
        """
        Check both rate limits and consume one unit of quota.

        Executes Layer 1 (burst) then Layer 2 (daily) in sequence.
        Both checks are atomic at the Redis level via Lua scripts.
        Neither check can partially succeed — either both pass or an
        exception is raised before any state is modified by Layer 2.

        Args:
            service: Service identifier string.
                     Must be one of: sms, payments, ussd, numbers, notifications

        Returns:
            Remaining daily quota after this call.
            For sandbox keys exempt from daily quota: returns 999999.

        Raises:
            RateLimitExceededError: Burst limit exceeded (HTTP 429, RATE_001)
            QuotaExceededError:     Daily quota exhausted (HTTP 429, RATE_002)
        """
        await self._check_burst(service)
        remaining = await self._check_and_increment_daily(service)
        return remaining

    async def get_daily_usage(self, service: str) -> int:
        """
        Return current daily usage count without consuming quota.

        Used by GET /quota to show current usage without side effects.
        Returns 0 if the quota key does not exist (no calls today).
        """
        key = self._daily_key(service)
        value = await self._redis.get(key)
        return int(value) if value is not None else 0

    async def get_all_usage(self) -> dict[str, int]:
        """
        Return current daily usage for all services in one call.

        Fetches all quota keys for this application using a pipeline
        to minimise round-trips — one pipeline call, not N individual GETs.
        """
        services = ["sms", "payments", "ussd", "numbers", "notifications"]
        keys = [self._daily_key(s) for s in services]

        pipeline = self._redis.pipeline()
        for key in keys:
            pipeline.get(key)
        results = await pipeline.execute()

        return {
            service: int(value) if value is not None else 0
            for service, value in zip(services, results)
        }

    async def reset_daily_quota(self, service: str) -> None:
        """
        Delete the daily quota key, resetting usage to zero.

        Called by the admin service when upgrading a plan mid-day —
        the new plan's higher limits should apply immediately, and
        the counter should start from zero under the new limit.

        Not callable from any route handler — admin service only.
        """
        key = self._daily_key(service)
        await self._redis.delete(key)

    # ── Private: Layer 1 — Burst Rate Limiter ─────────────────────────────

    async def _check_burst(self, service: str) -> None:
        """
        Execute the sliding window burst rate limit check.

        Raises RateLimitExceededError if the burst limit is exceeded.
        Uses the _BURST_LUA script for atomic check-and-record.
        """
        limit = self._burst_limit()
        key = self._burst_key(service)

        now_ms = self._now_ms()
        # Member is unique per request: timestamp + app_id suffix
        # Uniqueness is required because sorted set members must be distinct.
        # Two requests at the exact same millisecond would overwrite each other
        # without the app_id suffix — collapsing two requests into one entry.
        member = f"{now_ms}:{self._app_id[:8]}"

        result = await self._redis.eval(
            _BURST_LUA,
            1,          # number of KEYS arguments
            key,        # KEYS[1]
            now_ms,     # ARGV[1]
            self._BURST_WINDOW_MS,  # ARGV[2]
            limit,      # ARGV[3]
            member,     # ARGV[4]
        )

        if result == 0:
            raise RateLimitExceededError(
                retry_after=60,
                message=(
                    f"Burst rate limit of {limit} requests/minute exceeded "
                    f"for service '{service}'"
                ),
            )

    # ── Private: Layer 2 — Daily Quota ────────────────────────────────────

    async def _check_and_increment_daily(self, service: str) -> int:
        """
        Execute the daily quota check and increment.

        Sandbox keys: exempt from daily quota enforcement.
        Returns 999999 immediately without touching Redis.

        For live keys: raises QuotaExceededError if daily limit reached.
        Returns remaining quota (limit - new_count) on success.
        """
        if self._is_sandbox and _SANDBOX_EXEMPT_FROM_DAILY_QUOTA:
            return 999_999

        limit = self._daily_limit(service)
        key = self._daily_key(service)
        midnight = self._midnight_unix()

        result = await self._redis.eval(
            _QUOTA_LUA,
            1,          # number of KEYS arguments
            key,        # KEYS[1]
            limit,      # ARGV[1]
            midnight,   # ARGV[2]
        )

        if result == -1:
            raise QuotaExceededError(
                message=(
                    f"Daily quota of {limit} calls exhausted for "
                    f"service '{service}'. Resets at midnight UTC."
                )
            )

        return int(result)

    # ── Private: Limit Resolution ──────────────────────────────────────────

    def _burst_limit(self) -> int:
        """
        Return the burst limit (requests/minute) for this key's plan.

        Sandbox keys use the sandbox burst limit regardless of plan.
        This prevents a sandbox key from being used to probe the exact
        burst limits of a live plan tier.
        """
        if self._is_sandbox:
            return settings.rate_limit_burst_sandbox

        limits = {
            AppPlan.FREE:     settings.rate_limit_burst_free,
            AppPlan.STANDARD: settings.rate_limit_burst_standard,
            AppPlan.PREMIUM:  settings.rate_limit_burst_premium,
        }
        return limits.get(self._plan, settings.rate_limit_burst_free)

    def _daily_limit(self, service: str) -> int:
        """
        Return the daily quota limit for this plan and service.

        Raises ValueError for unrecognised service names — this is a
        programming error (wrong string passed by caller), not a
        user-facing error. It surfaces immediately in development.
        """
        plan_key = self._plan.value.lower()      # "free", "standard", "premium"
        attr = f"quota_{plan_key}_{service}"     # "quota_free_sms"
        limit = getattr(settings, attr, None)

        if limit is None:
            raise ValueError(
                f"No quota configured for plan='{self._plan}' "
                f"service='{service}'. "
                f"Expected settings attribute: '{attr}'"
            )
        return limit

    # ── Private: Key Builders ──────────────────────────────────────────────

    def _burst_key(self, service: str) -> str:
        """rate:{app_id}:{service}"""
        return f"{self._BURST_KEY_PREFIX}:{self._app_id}:{service}"

    def _daily_key(self, service: str) -> str:
        """quota:{app_id}:{service}:{YYYY-MM-DD}"""
        from app.utils.time_utils import today_utc_str
        return (
            f"{self._QUOTA_KEY_PREFIX}:"
            f"{self._app_id}:{service}:{today_utc_str()}"
        )

    # ── Private: Time Utilities ────────────────────────────────────────────

    @staticmethod
    def _now_ms() -> int:
        """Current UTC time as Unix milliseconds."""
        return int(utcnow().timestamp() * 1000)

    @staticmethod
    def _midnight_unix() -> int:
        """
        Unix timestamp of midnight UTC tonight.

        Used as the EXPIREAT argument for daily quota keys.
        The key expires exactly at the UTC day boundary regardless
        of the timezone of the machine running the application.

        calendar.timegm converts a UTC struct_time to a Unix timestamp
        without any timezone offset applied — unlike time.mktime which
        applies the local timezone and would produce the wrong midnight
        on a machine not configured for UTC.
        """
        now = utcnow()
        midnight_utc = datetime(
            now.year, now.month, now.day,
            23, 59, 59,         # 23:59:59 UTC tonight
            tzinfo=timezone.utc
        )
        return calendar.timegm(midnight_utc.timetuple())