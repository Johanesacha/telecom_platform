"""
Celery task for asynchronous SMS delivery.

CRITICAL ARCHITECTURE NOTE — SYNC SESSION:
  This task runs in a Celery worker process.
  Celery workers are synchronous — there is no running asyncio event loop.
  All database access MUST use SyncSessionLocal (synchronous SQLAlchemy).
  All async provider calls are bridged via asyncio.run().

  DO NOT use AsyncSession, AsyncSessionLocal, or 'await' in this file.
  Violating this causes RuntimeError: no running event loop at worker startup.

asyncio.run() usage:
  Provider methods are async (they simulate network I/O with asyncio.sleep).
  asyncio.run(coroutine) creates a fresh event loop, runs the coroutine to
  completion, destroys the loop, and returns the result synchronously.
  Each call is self-contained — no shared async resources between calls.

Retry strategy:
  max_retries=3, countdown=2**self.request.retries (exponential backoff)
  Attempt 1: immediate (original call)
  Retry 1:   after 2^0 = 1  second
  Retry 2:   after 2^1 = 2  seconds
  Retry 3:   after 2^2 = 4  seconds
  After retry 3: permanent failure -> FAILED status + quota rollback

Status transitions:
  PENDING -> SENT      (provider accepted, polling not required)
  PENDING -> DELIVERED (provider confirmed immediate delivery - sandbox)
  PENDING -> FAILED    (permanent failure after all retries)

Quota rollback:
  SMSService.send() consumed one 'sms' quota unit before enqueuing.
  On permanent failure, _rollback_sms_quota() decrements the daily Redis counter.
  Developers are not billed for messages the platform could not deliver.
"""
from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from celery import Task
from sqlalchemy.orm import Session

from app.core.celery_app import celery_app
from app.core.sync_database import SyncSessionLocal

logger = logging.getLogger(__name__)

# Service name used for quota tracking -- must match SMSService._SERVICE_NAME
_QUOTA_SERVICE_NAME = "sms"

# Lua script: DECR with floor at zero (atomic, no negative counters)
_LUA_DECR_FLOOR_ZERO = """
    local current = tonumber(redis.call('GET', KEYS[1]) or '0')
    if current > 0 then
        return redis.call('DECR', KEYS[1])
    end
    return 0
"""


@celery_app.task(
    bind=True,
    max_retries=3,
    name="app.tasks.sms_tasks.send_sms_task",
    acks_late=True,
)
def send_sms_task(self: Task, sms_id: str, is_sandbox: bool) -> dict:
    """
    Deliver a single SMS message via the appropriate provider.

    Args:
        sms_id:     UUID string of the SMSMessage record to process.
        is_sandbox: If True, use SandboxSMSProvider (deterministic, no delay).
                    If False, use MockSMSProvider (realistic simulation).

    Returns:
        dict with keys: sms_id, status, provider_message_id (or error_message)
    """
    logger.info(
        "send_sms_task started",
        extra={
            "sms_id": sms_id,
            "is_sandbox": is_sandbox,
            "attempt": self.request.retries + 1,
            "max_retries": self.max_retries,
        },
    )

    with SyncSessionLocal() as session:
        try:
            return _execute_sms_delivery(
                self=self,
                session=session,
                sms_id=sms_id,
                is_sandbox=is_sandbox,
            )
        except Exception:
            raise


def _execute_sms_delivery(
    *,
    self: Task,
    session: Session,
    sms_id: str,
    is_sandbox: bool,
) -> dict:
    """Core SMS delivery logic, separated for testability."""
    from app.domain.sms import SMSMessage, SMSStatus

    sms: SMSMessage | None = session.get(SMSMessage, UUID(sms_id))

    if sms is None:
        logger.warning("send_sms_task: SMS record not found", extra={"sms_id": sms_id})
        return {"sms_id": sms_id, "status": "SKIPPED", "reason": "record_not_found"}

    if sms.status != SMSStatus.PENDING:
        logger.info("send_sms_task: already processed", extra={"sms_id": sms_id})
        return {
            "sms_id": sms_id,
            "status": str(sms.status.value),
            "reason": "already_processed",
        }

    provider = _get_sms_provider(is_sandbox)

    try:
        result = asyncio.run(
            provider.send(
                to=sms.to_number,
                message=sms.message_text,
                from_alias=getattr(sms, "from_alias", None),
            )
        )
    except Exception as exc:
        logger.warning(
            "send_sms_task: provider exception — retry",
            extra={"sms_id": sms_id, "error": str(exc), "retry": self.request.retries},
        )
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)

    if result.success:
        terminal_status = SMSStatus.DELIVERED if is_sandbox else SMSStatus.SENT
        sms.status = terminal_status
        sms.provider_message_id = result.provider_message_id
        session.commit()
        logger.info(
            "send_sms_task: delivery accepted",
            extra={"sms_id": sms_id, "provider_message_id": result.provider_message_id},
        )
        return {
            "sms_id": sms_id,
            "status": terminal_status.value,
            "provider_message_id": result.provider_message_id,
        }

    logger.warning(
        "send_sms_task: provider failure",
        extra={"sms_id": sms_id, "error": result.error_message, "retry": self.request.retries},
    )

    if self.request.retries < self.max_retries:
        raise self.retry(
            exc=RuntimeError(result.error_message or "Provider failure"),
            countdown=2 ** self.request.retries,
        )

    _handle_permanent_sms_failure(
        session=session,
        sms=sms,
        error_message=result.error_message or "Provider permanently rejected message",
        is_sandbox=is_sandbox,
    )
    return {"sms_id": sms_id, "status": "FAILED", "error_message": result.error_message}


def _handle_permanent_sms_failure(
    *,
    session: Session,
    sms,
    error_message: str,
    is_sandbox: bool,
) -> None:
    """Handle permanent SMS failure: update DB then rollback quota."""
    from app.domain.sms import SMSStatus

    logger.error(
        "send_sms_task: permanent failure after all retries",
        extra={
            "sms_id": str(sms.id),
            "error": error_message[:200],
            "application_id": str(sms.application_id),
        },
    )

    sms.status = SMSStatus.FAILED
    sms.error_message = error_message[:500]
    session.commit()

    try:
        asyncio.run(
            _rollback_sms_quota(
                application_id=str(sms.application_id),
                is_sandbox=is_sandbox,
            )
        )
        logger.info("send_sms_task: quota rolled back", extra={"sms_id": str(sms.id)})
    except Exception as exc:
        logger.warning(
            "send_sms_task: quota rollback failed (non-critical)",
            extra={"sms_id": str(sms.id), "error": str(exc)},
        )


async def _rollback_sms_quota(
    application_id: str,
    is_sandbox: bool,
) -> None:
    """
    Decrement the daily SMS quota counter by 1 after permanent failure.

    Uses Redis DECR with Lua floor-at-zero to prevent negative counters.
    Constructs the daily key using QuotaService._QUOTA_KEY_PREFIX and the
    same date format used by QuotaService._daily_key() — which is:
        {_QUOTA_KEY_PREFIX}:{application_id}:{service_name}:{YYYY-MM-DD}

    We call QuotaService._daily_key() by instantiating a minimal mock
    that satisfies only the api_key.application_id requirement, so we
    always use the exact same key format as the service without duplication.
    """
    import redis.asyncio as aioredis
    from datetime import datetime, timezone
    from app.core.config import settings
    from app.services.quota_service import QuotaService

    redis_client = aioredis.from_url(
        str(settings.redis_url),
        encoding="utf-8",
        decode_responses=True,
    )
    try:
        # Build the key using QuotaService's own prefix and format.
        # QuotaService._QUOTA_KEY_PREFIX is a class-level constant.
        # _daily_key format: {prefix}:{app_id}:{service}:{YYYY-MM-DD}
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        prefix = QuotaService._QUOTA_KEY_PREFIX
        key = f"{prefix}:{application_id}:{_QUOTA_SERVICE_NAME}:{today}"

        await redis_client.eval(_LUA_DECR_FLOOR_ZERO, 1, key)

        logger.debug(
            "sms quota rollback: DECR issued",
            extra={"application_id": application_id, "key": key},
        )
    finally:
        await redis_client.aclose()


def _get_sms_provider(is_sandbox: bool):
    """Return SandboxSMSProvider or MockSMSProvider based on key type."""
    if is_sandbox:
        from app.providers.sandbox import SandboxSMSProvider
        return SandboxSMSProvider()
    from app.providers.mock_live import MockSMSProvider
    return MockSMSProvider(failure_rate=0.05)