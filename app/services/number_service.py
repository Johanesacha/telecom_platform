"""
NumberService — three-tier MSISDN verification with Redis caching.

Lookup order (strict):
  1. Redis cache (key: number:{app_id}:{e164}, TTL: 5 minutes)
     Hit  → return cached result immediately, no quota consumed
  2. Recent DB record (last 5 minutes for this app + e164)
     Hit  → rehydrate Redis, return result, no quota consumed
  3. Full verification via phonenumbers library
     → quota check → parse → DB create → cache write → commit → return

Sandbox: deterministic by last digit of E.164.
  Even digits (0,2,4,6,8) → is_active=True, operator resolved
  Odd digits  (1,3,5,7,9) → is_active=False, operator=UNKNOWN

Quota: consumed only on Tier 3 (genuine new verification).
Cache hits and DB rehydration do not consume quota.
"""
from __future__ import annotations

import json
from datetime import timedelta
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.api_key import ApiKey, KeyType
from app.domain.number import LineType, NumberVerification, OperatorEnum
from app.repositories.number_repo import NumberRepository
from app.services.quota_service import QuotaService
from app.utils.msisdn import MSISDNInfo, parse_msisdn
from app.utils.time_utils import utcnow

_SERVICE_NAME = "numbers"
_CACHE_TTL_SECONDS: int = 300          # 5 minutes
_RECENT_WINDOW_SECONDS: int = 300      # match cache TTL for consistency
_REDIS_KEY_PREFIX = "number"


class NumberService:
    """
    Validates and classifies MSISDN phone numbers.

    Three-tier lookup: Redis cache → recent DB → full verification.
    Only Tier 3 consumes quota and writes to the database.

    Usage in route handler:
        svc = NumberService(db, redis, api_key)
        result = await svc.verify(
            raw_msisdn="77 123 45 67",
            country_hint="SN",
            request_id=request.state.request_id,
        )
    """

    def __init__(
        self,
        session: AsyncSession,
        redis: aioredis.Redis,
        api_key: ApiKey,
    ) -> None:
        self._session = session
        self._redis = redis
        self._api_key = api_key
        self._app_id: UUID = api_key.application_id
        self._is_sandbox: bool = api_key.key_type == KeyType.SANDBOX
        self._repo = NumberRepository(session)
        self._quota = QuotaService(api_key, redis)

    # ── Public Interface ───────────────────────────────────────────────────

    async def verify(
        self,
        *,
        raw_msisdn: str,
        country_hint: str = "SN",
        request_id: str | None = None,
    ) -> dict:
        """
        Verify and classify an MSISDN through the three-tier chain.

        Returns a dict with verification results. Dict (not domain object)
        because Tier 1 returns a cached dict — all tiers must return
        the same shape for consistent response schema mapping.

        The return dict always contains:
            raw_input, msisdn_e164, is_valid, is_active,
            operator, line_type, country_code, national_format,
            country_iso, from_cache (bool), is_sandbox (bool)

        Raises:
            RateLimitExceededError: Burst limit exceeded.
            QuotaExceededError:     Daily quota exhausted.
            InvalidMSISDNError:     Number cannot be parsed or is invalid.
        """
        raw_msisdn = raw_msisdn.strip()

        # Pre-parse to validate and get E.164 for cache key.
        # If invalid, raises InvalidMSISDNError before touching quota/cache.
        try:
            preview = parse_msisdn(raw_msisdn, country_hint=country_hint)
        except Exception:
            raise

        e164 = preview.e164
        cache_key = self._cache_key(e164)

        # ── Tier 1: Redis cache check ──────────────────────────────────────
        cached_raw = await self._redis.get(cache_key)
        if cached_raw is not None:
            cached = json.loads(cached_raw)
            cached["from_cache"] = True
            return cached

        # ── Tier 2: Recent DB record check ────────────────────────────────
        since = utcnow() - timedelta(seconds=_RECENT_WINDOW_SECONDS)
        recent = await self._repo.get_recent_for_msisdn(
            self._app_id, e164, since=since
        )
        if recent is not None:
            result = _record_to_dict(recent, from_cache=False)
            # Rehydrate Redis so next call is a Tier 1 hit
            await self._redis.setex(
                cache_key,
                _CACHE_TTL_SECONDS,
                json.dumps(result),
            )
            result["from_cache"] = False
            return result

        # ── Tier 3: Full verification — consumes quota ─────────────────────
        await self._quota.check_and_consume(_SERVICE_NAME)

        if self._is_sandbox:
            result = _build_sandbox_result(preview)
        else:
            result = _build_live_result(preview)

        # Persist verification record
        await self._repo.create(
            application_id=self._app_id,
            raw_input=raw_msisdn,
            msisdn_e164=e164,
            country_hint=country_hint.upper(),
            is_valid=result["is_valid"],
            is_active=result["is_active"],
            operator=result["operator"],
            line_type=result["line_type"],
            country_code=result["country_code"],
            national_format=result["national_format"],
            request_id=request_id,
            is_sandbox=self._is_sandbox,
        )
        await self._session.commit()

        # Write to Redis cache AFTER commit
        result_for_cache = {**result}
        await self._redis.setex(
            cache_key,
            _CACHE_TTL_SECONDS,
            json.dumps(result_for_cache),
        )

        result["from_cache"] = False
        return result

    async def list_verifications(
        self,
        *,
        skip: int = 0,
        limit: int = 20,
        operator_filter: OperatorEnum | None = None,
        valid_only: bool = False,
    ) -> tuple[list[NumberVerification], int]:
        """
        Return paginated verification history for this application.

        Returns (items, total) for pagination metadata construction.
        """
        total = await self._repo.count_by_application(
            self._app_id,
            operator_filter=operator_filter,
            valid_only=valid_only,
        )
        items = await self._repo.list_by_application(
            self._app_id,
            skip=skip,
            limit=limit,
            operator_filter=operator_filter,
            valid_only=valid_only,
        )
        return items, total

    async def get_operator_breakdown(self) -> dict[str, int]:
        """
        Return verification counts grouped by operator.

        Used by the monitoring dashboard for operator distribution chart.
        """
        return await self._repo.count_by_operator(self._app_id)

    async def get_validity_breakdown(self) -> dict[str, int]:
        """
        Return valid vs invalid verification counts.

        Always returns both keys: {"valid": N, "invalid": M}.
        """
        return await self._repo.count_by_validity(self._app_id)

    # ── Private Helpers ────────────────────────────────────────────────────

    def _cache_key(self, e164: str) -> str:
        """
        Build Redis cache key for a verified MSISDN.

        Format: number:{app_id}:{e164}

        Scoped to app_id: two applications verifying the same number
        produce independent cache entries.
        """
        return f"{_REDIS_KEY_PREFIX}:{self._app_id}:{e164}"


# ── Module-level pure functions ────────────────────────────────────────────

def _build_live_result(info: MSISDNInfo) -> dict:
    """
    Build a verification result dict from a parsed MSISDNInfo (live path).

    is_active is True for valid mobile numbers — no real HLR lookup
    is performed. Production would call an HLR service here.
    """
    return {
        "raw_input": info.e164,
        "msisdn_e164": info.e164,
        "is_valid": True,
        "is_active": info.is_mobile,
        "operator": info.operator,
        "line_type": LineType.MOBILE if info.is_mobile else LineType.FIXED,
        "country_code": info.country_code,
        "national_format": info.national,
        "country_iso": info.country_iso,
        "is_sandbox": False,
    }


def _build_sandbox_result(info: MSISDNInfo) -> dict:
    """
    Build a deterministic sandbox result based on last digit of E.164.

    Even last digit (0,2,4,6,8) → is_active=True, operator resolved.
    Odd last digit  (1,3,5,7,9) → is_active=False, operator=UNKNOWN.

    Documented in API reference for predictable integration tests.
    """
    last_digit = next(
        (ch for ch in reversed(info.e164) if ch.isdigit()),
        "0",
    )
    is_active = int(last_digit) % 2 == 0
    operator = info.operator if is_active else OperatorEnum.UNKNOWN

    return {
        "raw_input": info.e164,
        "msisdn_e164": info.e164,
        "is_valid": True,
        "is_active": is_active,
        "operator": operator,
        "line_type": LineType.MOBILE if info.is_mobile else LineType.FIXED,
        "country_code": info.country_code,
        "national_format": info.national,
        "country_iso": info.country_iso,
        "is_sandbox": True,
    }


def _record_to_dict(record: NumberVerification, *, from_cache: bool) -> dict:
    """
    Convert a NumberVerification DB record to the standard result dict.

    Used when rehydrating from a Tier 2 DB hit. Same shape as
    _build_live_result() and _build_sandbox_result().
    """
    return {
        "raw_input": record.raw_input,
        "msisdn_e164": record.msisdn_e164,
        "is_valid": record.is_valid,
        "is_active": record.is_active,
        "operator": record.operator,
        "line_type": record.line_type,
        "country_code": record.country_code,
        "national_format": record.national_format,
        "country_iso": record.country_hint,
        "is_sandbox": record.is_sandbox,
        "from_cache": from_cache,
    }