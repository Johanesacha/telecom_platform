"""
NotificationService — channel-aware notification dispatch.

Channel routing:
  SMS   → Repository create + mock provider (no SMSService to avoid circular import)
  EMAIL → Repository create + mock SMTP provider
  PUSH  → Repository create + placeholder (FCM/APNS out of scope)

Circular import prevention:
  SMS channel does NOT use SMSService. It writes to NotificationRepository
  directly. Notification SMSes are internal platform operations — they do
  not consume developer quota, do not go through the Celery send chain,
  and are not tracked in sms_messages table.

Quota:
  Consumed for EMAIL and PUSH channels.
  SMS channel is an internal platform cost — not billed to the developer.

Idempotency:
  Checked before dispatch, same pattern as SMSService and PaymentService.
"""
from __future__ import annotations

import json
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ResourceNotFoundError
from app.domain.api_key import ApiKey, KeyType
from app.domain.notification import (
    NotificationChannel,
    NotificationRecord,
    NotificationStatus,
)
from app.repositories.notification_repo import NotificationRepository
from app.services.quota_service import QuotaService
from app.utils.idempotency import cache_response, get_cached_response
from app.utils.time_utils import utcnow

_SERVICE_NAME = "notifications"


class NotificationService:
    """
    Dispatches notifications across SMS, EMAIL, and PUSH channels.

    Instantiate per-request with session, redis, and api_key.

    Usage in route handler:
        svc = NotificationService(db, redis, api_key)
        record, is_duplicate = await svc.dispatch(
            channel=NotificationChannel.EMAIL,
            recipient="dev@example.com",
            body="Your API quota is at 80%.",
            subject="Quota Alert",
            idempotency_key=request.idempotency_key,
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
        self._repo = NotificationRepository(session)
        self._quota = QuotaService(api_key, redis)

    # ── Public Interface ───────────────────────────────────────────────────

    async def dispatch(
        self,
        *,
        channel: NotificationChannel,
        recipient: str,
        body: str,
        subject: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
    ) -> tuple[NotificationRecord | dict, bool]:
        """
        Dispatch a notification through the specified channel.

        Ordering:
          1. Idempotency check
          2. Quota check (EMAIL and PUSH only — SMS is internal)
          3. Create PENDING record
          4. Dispatch via channel-specific handler
          5. Update status to SENT or FAILED
          6. Cache idempotency response
          7. Commit

        Returns:
            (record_or_cached, is_duplicate)
        """
        # ── Step 1: Idempotency check ──────────────────────────────────────
        if idempotency_key is not None:
            cached = await get_cached_response(
                self._redis, self._app_id, _SERVICE_NAME, idempotency_key
            )
            if cached is not None:
                return cached, True

        # ── Step 2: Quota check (channel-conditional) ──────────────────────
        # SMS channel is an internal platform operation — not billed to
        # the developer's notifications quota. EMAIL and PUSH are billed.
        if channel != NotificationChannel.SMS:
            await self._quota.check_and_consume(_SERVICE_NAME)

        # ── Step 3: Create PENDING record ──────────────────────────────────
        record = await self._repo.create(
            application_id=self._app_id,
            channel=channel,
            recipient=recipient,
            body=body,
            subject=subject,
            status=NotificationStatus.PENDING,
            idempotency_key=idempotency_key,
            request_id=request_id,
            is_sandbox=self._is_sandbox,
        )

        # ── Step 4 + 5: Dispatch and update status ─────────────────────────
        final_record = await self._route_and_dispatch(record)

        # ── Step 6: Cache idempotency response ────────────────────────────
        if idempotency_key is not None:
            await cache_response(
                self._redis, self._app_id, _SERVICE_NAME,
                idempotency_key,
                _serialise_record(final_record),
            )

        # ── Step 7: Commit ─────────────────────────────────────────────────
        await self._session.commit()
        return final_record, False

    async def get_record(
        self,
        *,
        record_id: UUID,
    ) -> NotificationRecord:
        """
        Fetch a single notification record owned by this application.

        Raises ResourceNotFoundError if not found or wrong application.
        """
        record = await self._repo.get_by_id_for_application(
            record_id, self._app_id
        )
        if record is None:
            raise ResourceNotFoundError(
                f"Notification record {record_id} not found"
            )
        return record

    async def list_history(
        self,
        *,
        skip: int = 0,
        limit: int = 20,
        channel_filter: NotificationChannel | None = None,
        status_filter: NotificationStatus | None = None,
    ) -> tuple[list[NotificationRecord], int]:
        """
        Return paginated notification history for this application.

        Returns (items, total) for pagination metadata.
        """
        total = await self._repo.count_by_application(
            self._app_id,
            channel_filter=channel_filter,
            status_filter=status_filter,
        )
        items = await self._repo.list_by_application(
            self._app_id,
            skip=skip,
            limit=limit,
            channel_filter=channel_filter,
            status_filter=status_filter,
        )
        return items, total

    async def get_delivery_matrix(self) -> dict[str, dict[str, int]]:
        """
        Return notification counts by channel and status.

        Returns nested dict: {channel: {status: count}}.
        Missing combinations are absent — caller fills zeros.
        """
        return await self._repo.count_by_channel_and_status(self._app_id)

    # ── Private: Channel Routing ───────────────────────────────────────────

    async def _route_and_dispatch(
        self,
        record: NotificationRecord,
    ) -> NotificationRecord:
        """
        Route a PENDING notification record to its channel handler.

        Each handler attempts dispatch, then calls update_status()
        with SENT on success or FAILED on provider error.
        """
        if record.channel == NotificationChannel.SMS:
            return await self._dispatch_sms(record)
        elif record.channel == NotificationChannel.EMAIL:
            return await self._dispatch_email(record)
        elif record.channel == NotificationChannel.PUSH:
            return await self._dispatch_push(record)
        else:
            return await self._repo.update_status(
                record,
                NotificationStatus.FAILED,
                error_message=f"Unsupported channel: {record.channel}",
            )

    async def _dispatch_sms(
        self,
        record: NotificationRecord,
    ) -> NotificationRecord:
        """
        Dispatch an SMS notification.

        Does NOT use SMSService — avoids circular import.
        Does NOT write to sms_messages table — only notification_records.
        Does NOT consume SMS quota — internal platform operation.

        Sandbox: always SENT. Live: mock provider call.
        """
        if self._is_sandbox:
            return await self._repo.update_status(
                record,
                NotificationStatus.SENT,
                provider_message_id=f"sandbox-sms-{record.id}",
            )

        try:
            provider_id = await _mock_sms_provider(
                to=record.recipient,
                body=record.body,
            )
            return await self._repo.update_status(
                record,
                NotificationStatus.SENT,
                provider_message_id=provider_id,
            )
        except Exception as exc:
            return await self._repo.update_status(
                record,
                NotificationStatus.FAILED,
                error_message=str(exc)[:500],
            )

    async def _dispatch_email(
        self,
        record: NotificationRecord,
    ) -> NotificationRecord:
        """
        Dispatch an EMAIL notification.

        Sandbox: always SENT. Live: mock SMTP provider.
        Production: SendGrid/Mailgun/SES via httpx.
        """
        if self._is_sandbox:
            return await self._repo.update_status(
                record,
                NotificationStatus.SENT,
                provider_message_id=f"sandbox-email-{record.id}",
            )

        try:
            provider_id = await _mock_email_provider(
                to=record.recipient,
                subject=record.subject or "(no subject)",
                body=record.body,
            )
            return await self._repo.update_status(
                record,
                NotificationStatus.SENT,
                provider_message_id=provider_id,
            )
        except Exception as exc:
            return await self._repo.update_status(
                record,
                NotificationStatus.FAILED,
                error_message=str(exc)[:500],
            )

    async def _dispatch_push(
        self,
        record: NotificationRecord,
    ) -> NotificationRecord:
        """
        Dispatch a PUSH notification.

        FCM/APNS integration is out of scope for this platform version.
        Sandbox always succeeds. Live path is a documented placeholder.
        """
        if self._is_sandbox:
            return await self._repo.update_status(
                record,
                NotificationStatus.SENT,
                provider_message_id=f"sandbox-push-{record.id}",
            )

        return await self._repo.update_status(
            record,
            NotificationStatus.FAILED,
            error_message=(
                "PUSH channel is not yet implemented in this platform version. "
                "Use SMS or EMAIL channel."
            ),
        )


# ── Mock provider functions ────────────────────────────────────────────────

async def _mock_sms_provider(*, to: str, body: str) -> str:
    """Simulate an SMSC provider API call. Returns fake provider message ID."""
    import secrets
    return f"sms-prov-{secrets.token_hex(8)}"


async def _mock_email_provider(*, to: str, subject: str, body: str) -> str:
    """Simulate an SMTP/API email provider call. Returns fake message ID."""
    import secrets
    return f"email-prov-{secrets.token_hex(8)}"


def _serialise_record(record: NotificationRecord) -> dict:
    """Convert a NotificationRecord to a JSON-safe dict for idempotency cache."""
    return {
        "id": str(record.id),
        "channel": record.channel,
        "recipient": record.recipient,
        "status": record.status,
        "provider_message_id": record.provider_message_id,
        "is_sandbox": record.is_sandbox,
        "created_at": record.created_at.isoformat()
        if record.created_at else None,
    }