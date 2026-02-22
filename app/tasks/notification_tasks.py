"""
Celery task for asynchronous notification delivery.

Same sync/async architecture as sms_tasks.py:
  - SYNC SQLAlchemy session throughout
  - asyncio.run() wraps all async provider calls
  - bind=True, max_retries=3, exponential backoff
  - Permanent failure -> FAILED status + quota rollback (EMAIL and PUSH only)

Channel routing:
  The task receives channel as a string argument.
  Channel string is uppercased inside _execute_notification_delivery().

Quota rollback rules:
  SMS channel:   no rollback -- SMS notifications are NOT quota-charged
  EMAIL channel: rollback one 'notifications' quota unit
  PUSH channel:  rollback one 'notifications' quota unit

The task does not import app.schemas -- tasks are internal infrastructure.
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

_QUOTA_SERVICE_NAME = "notifications"

# Channels that consume quota (SMS is exempt)
_QUOTA_CHARGED_CHANNELS = {"EMAIL", "PUSH"}

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
    name="app.tasks.notification_tasks.send_notification_task",
    acks_late=True,
)
def send_notification_task(
    self: Task,
    notification_id: str,
    channel: str,
    is_sandbox: bool,
) -> dict:
    """
    Deliver a single notification via the appropriate provider and channel.

    Args:
        notification_id: UUID string of the NotificationRecord to process.
        channel:         'SMS', 'EMAIL', or 'PUSH'.
        is_sandbox:      If True, use SandboxNotificationProvider.
    """
    logger.info(
        "send_notification_task started",
        extra={
            "notification_id": notification_id,
            "channel": channel,
            "is_sandbox": is_sandbox,
            "attempt": self.request.retries + 1,
        },
    )

    with SyncSessionLocal() as session:
        try:
            return _execute_notification_delivery(
                self=self,
                session=session,
                notification_id=notification_id,
                channel=channel.upper(),
                is_sandbox=is_sandbox,
            )
        except Exception:
            raise


def _execute_notification_delivery(
    *,
    self: Task,
    session: Session,
    notification_id: str,
    channel: str,
    is_sandbox: bool,
) -> dict:
    """Core notification delivery logic, separated for testability."""
    from app.domain.notification import NotificationRecord, NotificationStatus

    record: NotificationRecord | None = session.get(
        NotificationRecord, UUID(notification_id)
    )

    if record is None:
        logger.warning(
            "send_notification_task: record not found",
            extra={"notification_id": notification_id},
        )
        return {
            "notification_id": notification_id,
            "status": "SKIPPED",
            "reason": "record_not_found",
        }

    if record.status != NotificationStatus.PENDING:
        logger.info(
            "send_notification_task: already processed",
            extra={"notification_id": notification_id},
        )
        return {
            "notification_id": notification_id,
            "channel": channel,
            "status": record.status.value
                if hasattr(record.status, "value") else str(record.status),
            "reason": "already_processed",
        }

    provider = _get_notification_provider(is_sandbox)

    try:
        result = asyncio.run(
            provider.send(
                channel=channel,
                recipient=record.recipient,
                body=record.body,
                subject=getattr(record, "subject", None),
            )
        )
    except Exception as exc:
        logger.warning(
            "send_notification_task: provider exception — retry",
            extra={
                "notification_id": notification_id,
                "channel": channel,
                "error": str(exc),
                "retry": self.request.retries,
            },
        )
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)

    if result.success:
        record.status = NotificationStatus.SENT
        record.provider_message_id = result.provider_message_id
        session.commit()
        logger.info(
            "send_notification_task: delivery accepted",
            extra={
                "notification_id": notification_id,
                "channel": channel,
                "provider_message_id": result.provider_message_id,
            },
        )
        return {
            "notification_id": notification_id,
            "channel": channel,
            "status": "SENT",
            "provider_message_id": result.provider_message_id,
        }

    logger.warning(
        "send_notification_task: provider failure",
        extra={
            "notification_id": notification_id,
            "channel": channel,
            "error": result.error_message,
            "retry": self.request.retries,
        },
    )

    if self.request.retries < self.max_retries:
        raise self.retry(
            exc=RuntimeError(result.error_message or "Provider failure"),
            countdown=2 ** self.request.retries,
        )

    _handle_permanent_notification_failure(
        session=session,
        record=record,
        error_message=result.error_message or "Provider permanently rejected notification",
        channel=channel,
        is_sandbox=is_sandbox,
    )
    return {
        "notification_id": notification_id,
        "channel": channel,
        "status": "FAILED",
        "error_message": result.error_message,
    }


def _handle_permanent_notification_failure(
    *,
    session: Session,
    record,
    error_message: str,
    channel: str,
    is_sandbox: bool,
) -> None:
    """Handle permanent failure: update DB then rollback quota if applicable."""
    from app.domain.notification import NotificationStatus

    logger.error(
        "send_notification_task: permanent failure after all retries",
        extra={
            "notification_id": str(record.id),
            "channel": channel,
            "error": error_message[:200],
            "application_id": str(record.application_id),
        },
    )

    record.status = NotificationStatus.FAILED
    record.error_message = error_message[:500]
    session.commit()

    if channel.upper() in _QUOTA_CHARGED_CHANNELS:
        try:
            asyncio.run(
                _rollback_notification_quota(
                    application_id=str(record.application_id),
                    is_sandbox=is_sandbox,
                )
            )
            logger.info(
                "send_notification_task: quota rolled back",
                extra={"notification_id": str(record.id), "channel": channel},
            )
        except Exception as exc:
            logger.warning(
                "send_notification_task: quota rollback failed (non-critical)",
                extra={"notification_id": str(record.id), "error": str(exc)},
            )
    else:
        logger.debug(
            "send_notification_task: SMS channel -- no quota to roll back",
            extra={"notification_id": str(record.id)},
        )


async def _rollback_notification_quota(
    application_id: str,
    is_sandbox: bool,
) -> None:
    """
    Decrement the daily notifications quota counter by 1 after permanent failure.

    Called only for EMAIL and PUSH channels.
    Uses the same key format as QuotaService._daily_key():
        {_QUOTA_KEY_PREFIX}:{app_id}:{service_name}:{YYYY-MM-DD}
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
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        prefix = QuotaService._QUOTA_KEY_PREFIX
        key = f"{prefix}:{application_id}:{_QUOTA_SERVICE_NAME}:{today}"

        await redis_client.eval(_LUA_DECR_FLOOR_ZERO, 1, key)

        logger.debug(
            "notification quota rollback: DECR issued",
            extra={"application_id": application_id, "key": key},
        )
    finally:
        await redis_client.aclose()


def _get_notification_provider(is_sandbox: bool):
    """Return SandboxNotificationProvider or MockNotificationProvider."""
    if is_sandbox:
        from app.providers.sandbox import SandboxNotificationProvider
        return SandboxNotificationProvider()
    from app.providers.mock_live import MockNotificationProvider
    return MockNotificationProvider(failure_rate=0.05)