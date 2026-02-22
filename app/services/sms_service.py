"""
SMSService — full chain: quota → idempotency → create → Celery.

Chain ordering is strictly enforced and non-negotiable:
  1. Idempotency check (cache) — before quota consumption
  2. Quota check-and-consume — before any DB write
  3. Input validation and normalisation
  4. Database create (with IntegrityError race handling)
  5. Cache idempotency response
  6. session.commit()
  7. Celery task enqueue — after commit, never before

Quota rollback: exposed via rollback_quota() for Celery task to call
on permanent failure (all retries exhausted).

Segment calculation: GSM-7 aware — counts extended chars as 2,
switches to UCS-2 if any char falls outside GSM-7 charset.
"""
from __future__ import annotations

import json
import secrets
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    InvalidMSISDNError,
    MessageTooLongError,
    ResourceNotFoundError,
)
from app.domain.api_key import ApiKey, KeyType
from app.domain.sms import SMSMessage, SMSStatus
from app.repositories.sms_repo import SMSRepository
from app.services.quota_service import QuotaService
from app.utils.idempotency import (
    build_cache_key,
    cache_response,
    get_cached_response,
)
from app.utils.msisdn import parse_msisdn
from app.utils.time_utils import utcnow


# ── GSM-7 Character Sets ───────────────────────────────────────────────────

# Basic GSM-7 charset — 128 characters, each counts as 1
_GSM7_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./"
    "0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "ÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyz"
    "äöñüà"
)

# Extended GSM-7 characters — each counts as 2 characters (escape + char)
_GSM7_EXTENDED = frozenset("^{}\\[~]|€")

# SMS length limits
_GSM7_SINGLE_MAX: int = 160
_GSM7_SEGMENT_MAX: int = 153      # per segment in multipart
_UCS2_SINGLE_MAX: int = 70
_UCS2_SEGMENT_MAX: int = 67       # per segment in multipart

# Maximum segments allowed — 8 segments × 153 = 1224 GSM-7 chars
_MAX_SEGMENTS: int = 8

_SERVICE_NAME = "sms"


class SMSService:
    """
    Orchestrates the complete SMS send chain.

    Instantiate per-request with session, redis, and the authenticated api_key.
    The api_key carries application_id, plan, and key_type (LIVE/SANDBOX).

    Usage in route handler:
        svc = SMSService(db, redis, api_key)
        message, is_duplicate = await svc.send(
            to_number="+221771234567",
            message_text="Hello world",
            from_alias="TelecomPF",
            idempotency_key=request.idempotency_key,
        )
        status_code = 200 if is_duplicate else 202
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
        self._repo = SMSRepository(session)
        self._quota = QuotaService(api_key, redis)

    # ── Public Interface ───────────────────────────────────────────────────

    async def send(
        self,
        *,
        to_number: str,
        message_text: str,
        from_alias: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
    ) -> tuple[SMSMessage | dict, bool]:
        """
        Execute the full SMS send chain.

        Returns:
            (message_or_cached, is_duplicate)
            is_duplicate=True  → return HTTP 200, no quota consumed
            is_duplicate=False → return HTTP 202, quota consumed, task enqueued

        Raises:
            RateLimitExceededError: Burst limit exceeded.
            QuotaExceededError:     Daily quota exhausted.
            InvalidMSISDNError:     to_number is not a valid phone number.
            MessageTooLongError:    message_text exceeds maximum segment limit.
        """
        # ── Step 1: Idempotency check BEFORE quota ─────────────────────────
        if idempotency_key is not None:
            cached = await get_cached_response(
                self._redis, self._app_id, _SERVICE_NAME, idempotency_key
            )
            if cached is not None:
                return cached, True

        # ── Step 2: Quota check — consumes one unit ────────────────────────
        await self._quota.check_and_consume(_SERVICE_NAME)

        # ── Step 3: Validate and normalise inputs ──────────────────────────
        msisdn_info = parse_msisdn(to_number)
        e164_number = msisdn_info.e164
        segment_count = calculate_segments(message_text)

        if segment_count > _MAX_SEGMENTS:
            raise MessageTooLongError(
                f"Message requires {segment_count} segments. "
                f"Maximum allowed is {_MAX_SEGMENTS} "
                f"({_MAX_SEGMENTS * _GSM7_SEGMENT_MAX} GSM-7 characters)."
            )

        # ── Step 4: Database write with race condition handling ────────────
        message, is_duplicate = await self._create_with_race_protection(
            to_number=e164_number,
            message_text=message_text,
            from_alias=from_alias,
            segment_count=segment_count,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )

        if is_duplicate:
            await self._quota_rollback()
            return message, True

        # ── Step 5: Cache idempotency response ────────────────────────────
        if idempotency_key is not None:
            await cache_response(
                self._redis, self._app_id, _SERVICE_NAME,
                idempotency_key,
                _serialise_message(message),
            )

        # ── Step 6: Commit ─────────────────────────────────────────────────
        await self._session.commit()

        # ── Step 7: Enqueue Celery task AFTER commit ───────────────────────
        if not self._is_sandbox:
            from app.tasks.sms_tasks import send_sms_task
            send_sms_task.delay(str(message.id))
        else:
            sandbox_status = _sandbox_status_for(e164_number)
            await self._repo.update_status(message, sandbox_status)
            await self._session.commit()

        return message, False

    async def get_message_status(
        self,
        *,
        message_id: UUID,
    ) -> SMSMessage:
        """
        Fetch a single SMS record owned by this service's application.

        Raises:
            ResourceNotFoundError: If the message does not exist or belongs
                                   to a different application.
        """
        message = await self._repo.get_by_id_for_application(
            message_id, self._app_id
        )
        if message is None:
            raise ResourceNotFoundError(
                f"SMS message {message_id} not found"
            )
        return message

    async def list_history(
        self,
        *,
        skip: int = 0,
        limit: int = 20,
        status_filter: SMSStatus | None = None,
    ) -> tuple[list[SMSMessage], int]:
        """
        Return paginated SMS history for this application.

        Returns:
            (items, total) — total is the unfiltered count for pagination.
        """
        total = await self._repo.count_by_application(
            self._app_id, status_filter=status_filter
        )
        items = await self._repo.list_by_application(
            self._app_id,
            skip=skip,
            limit=limit,
            status_filter=status_filter,
        )
        return items, total

    async def rollback_quota(self) -> None:
        """
        Decrement the daily SMS quota by one unit.

        Called by the Celery send_sms_task on permanent failure
        (all retries exhausted, provider definitively unreachable).
        """
        await self._quota_rollback()

    # ── Private Helpers ────────────────────────────────────────────────────

    async def _create_with_race_protection(
        self,
        *,
        to_number: str,
        message_text: str,
        from_alias: str | None,
        segment_count: int,
        idempotency_key: str | None,
        request_id: str | None,
    ) -> tuple[SMSMessage, bool]:
        """
        Create an SMSMessage record, handling concurrent duplicate inserts.
        """
        try:
            message = await self._repo.create(
                application_id=self._app_id,
                to_number=to_number,
                from_alias=from_alias,
                message_text=message_text,
                status=SMSStatus.PENDING,
                segment_count=segment_count,
                idempotency_key=idempotency_key,
                request_id=request_id,
                is_sandbox=self._is_sandbox,
            )
            return message, False

        except IntegrityError:
            await self._session.rollback()

            if idempotency_key is None:
                raise

            existing = await self._repo.get_by_idempotency_key(
                self._app_id, idempotency_key
            )
            if existing is None:
                raise

            return existing, True

    async def _quota_rollback(self) -> None:
        """Decrement daily quota counter by one without limit check."""
        from app.utils.time_utils import today_utc_str
        key = f"quota:{self._app_id}:{_SERVICE_NAME}:{today_utc_str()}"
        await self._redis.decr(key)


# ── Module-level pure functions ────────────────────────────────────────────

def calculate_segments(text: str) -> int:
    """
    Calculate the number of SMS segments required for a message.

    GSM-7 encoding (default for ASCII-like messages):
      Single SMS:     160 characters
      Multipart SMS:  153 characters per segment

    UCS-2 encoding (required when any character is outside GSM-7):
      Single SMS:     70 characters
      Multipart SMS:  67 characters per segment

    Extended GSM-7 characters (^{}\\[~]|€) count as 2 characters each.
    """
    if not text:
        return 1

    is_gsm7 = all(
        ch in _GSM7_BASIC or ch in _GSM7_EXTENDED
        for ch in text
    )

    if is_gsm7:
        effective_length = sum(
            2 if ch in _GSM7_EXTENDED else 1
            for ch in text
        )
        single_max = _GSM7_SINGLE_MAX
        segment_max = _GSM7_SEGMENT_MAX
    else:
        effective_length = len(text)
        single_max = _UCS2_SINGLE_MAX
        segment_max = _UCS2_SEGMENT_MAX

    if effective_length <= single_max:
        return 1

    return (effective_length + segment_max - 1) // segment_max


def _sandbox_status_for(e164_number: str) -> SMSStatus:
    """
    Deterministic sandbox outcome based on last digit of E.164 number.

    Last digit 8 → FAILED
    All others  → DELIVERED
    """
    last_digit = next(
        (ch for ch in reversed(e164_number) if ch.isdigit()),
        "0",
    )
    return SMSStatus.FAILED if last_digit == "8" else SMSStatus.DELIVERED


def _serialise_message(message: SMSMessage) -> dict:
    """Convert an SMSMessage to a JSON-safe dict for idempotency caching."""
    return {
        "id": str(message.id),
        "to_number": message.to_number,
        "status": message.status,
        "segment_count": message.segment_count,
        "is_sandbox": message.is_sandbox,
        "created_at": message.created_at.isoformat()
        if message.created_at else None,
    }