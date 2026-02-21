"""
NotificationRecord repository.

One record per notification dispatch attempt.
Write pattern: create() on dispatch, update_status() once on provider response.
Records are never deleted and never updated beyond the single status transition.

Primary read consumers:
  - Monitoring dashboard: aggregate counts by channel and status
  - Application history endpoint: paginated list with optional channel filter
  - Idempotency check: lookup by key before creation

The composite index ix_notification_app_channel_status serves
GROUP BY queries on (channel, status) for monitoring aggregates.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select

from app.domain.notification import (
    NotificationChannel,
    NotificationRecord,
    NotificationStatus,
)
from app.repositories.base import BaseRepository


class NotificationRepository(BaseRepository[NotificationRecord]):

    def __init__(self, session) -> None:
        super().__init__(NotificationRecord, session)

    # ── Idempotency ────────────────────────────────────────────────────────

    async def get_by_idempotency_key(
        self,
        app_id: UUID,
        idempotency_key: str,
    ) -> NotificationRecord | None:
        """
        Look up a notification record by idempotency key, scoped to
        the calling application.

        Scoping to app_id is mandatory — two applications may use the
        same idempotency key string without conflict. Without this scope,
        Application B could receive Application A's notification record.

        Called before every notification dispatch when the client
        provides an Idempotency-Key header. If a record is returned,
        the service layer returns it immediately as HTTP 200 without
        re-dispatching.

        Returns None if this is a genuinely new request.
        """
        stmt = (
            select(NotificationRecord)
            .where(
                NotificationRecord.application_id == app_id,
                NotificationRecord.idempotency_key == idempotency_key,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    # ── Single Record Lookups ──────────────────────────────────────────────

    async def get_by_id_for_application(
        self,
        record_id: UUID,
        app_id: UUID,
    ) -> NotificationRecord | None:
        """
        Fetch a single notification record by ID, restricted to the
        calling application's ownership.

        Enforces resource isolation at the query level. Returning None
        for both 'not found' and 'wrong owner' prevents leaking
        information about whether a record with that ID exists at all.
        """
        stmt = (
            select(NotificationRecord)
            .where(
                NotificationRecord.id == record_id,
                NotificationRecord.application_id == app_id,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    # ── Paginated History ──────────────────────────────────────────────────

    async def list_by_application(
        self,
        app_id: UUID,
        *,
        skip: int = 0,
        limit: int = 20,
        channel_filter: NotificationChannel | None = None,
        status_filter: NotificationStatus | None = None,
    ) -> list[NotificationRecord]:
        """
        Return paginated notification history for an application.

        Ordered by created_at DESC — most recent dispatch first.

        Both channel_filter and status_filter are optional and
        independent. Both can be applied simultaneously:
            channel=EMAIL + status=FAILED → failed email dispatches only.

        The composite index ix_notification_app_channel_status is used
        when both channel and status filters are applied. With only
        application_id, the standalone index on application_id is used.

        Args:
            app_id:         Restrict to this application's records.
            skip:           Pagination offset.
            limit:          Page size. Service layer enforces a maximum.
            channel_filter: Optional — narrow to one channel (SMS/EMAIL/PUSH).
            status_filter:  Optional — narrow to one status.
        """
        stmt = (
            select(NotificationRecord)
            .where(NotificationRecord.application_id == app_id)
            .order_by(NotificationRecord.created_at.desc())
            .offset(skip)
            .limit(limit)
        )

        if channel_filter is not None:
            stmt = stmt.where(
                NotificationRecord.channel == channel_filter
            )

        if status_filter is not None:
            stmt = stmt.where(
                NotificationRecord.status == status_filter
            )

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_by_application(
        self,
        app_id: UUID,
        *,
        channel_filter: NotificationChannel | None = None,
        status_filter: NotificationStatus | None = None,
    ) -> int:
        """
        Return total notification count for an application.

        Accepts identical filters to list_by_application() so the
        count reflects the exact same filtered set shown on the page.
        Pair with list_by_application() to build pagination metadata.
        """
        stmt = (
            select(func.count())
            .select_from(NotificationRecord)
            .where(NotificationRecord.application_id == app_id)
        )

        if channel_filter is not None:
            stmt = stmt.where(
                NotificationRecord.channel == channel_filter
            )

        if status_filter is not None:
            stmt = stmt.where(
                NotificationRecord.status == status_filter
            )

        result = await self.session.execute(stmt)
        return result.scalar_one()

    # ── Analytics ──────────────────────────────────────────────────────────

    async def count_by_channel_and_status(
        self,
        app_id: UUID,
    ) -> dict[str, dict[str, int]]:
        """
        Return notification counts grouped by channel and status.

        Issues a single GROUP BY query — not N×M separate count queries.
        Used by the monitoring dashboard to show a delivery matrix:

            {
                "SMS":   {"SENT": 847, "FAILED": 12, "PENDING": 3},
                "EMAIL": {"SENT": 201, "FAILED": 4,  "PENDING": 0},
                "PUSH":  {"SENT": 543, "FAILED": 31, "PENDING": 1},
            }

        Missing combinations (channel+status with zero records) are
        absent from the nested dict. The service layer fills in zeros
        when building the response schema.

        The composite index ix_notification_app_channel_status is
        designed for exactly this query pattern.
        """
        stmt = (
            select(
                NotificationRecord.channel,
                NotificationRecord.status,
                func.count().label("total"),
            )
            .where(NotificationRecord.application_id == app_id)
            .group_by(
                NotificationRecord.channel,
                NotificationRecord.status,
            )
        )
        result = await self.session.execute(stmt)

        # Build nested dict: {channel: {status: count}}
        breakdown: dict[str, dict[str, int]] = {}
        for row in result.all():
            channel = row.channel
            status = row.status
            count = row.total
            if channel not in breakdown:
                breakdown[channel] = {}
            breakdown[channel][status] = count

        return breakdown

    async def count_failed_since(
        self,
        app_id: UUID,
        since: datetime,
        *,
        channel: NotificationChannel | None = None,
    ) -> int:
        """
        Return the count of FAILED notification records since a given
        UTC datetime, optionally filtered by channel.

        Used by the monitoring service to compute per-channel
        failure rates within a rolling time window.

        Args:
            app_id:  Restrict to this application.
            since:   UTC datetime lower bound (timezone-aware).
            channel: Optional — restrict to one channel.
        """
        stmt = (
            select(func.count())
            .select_from(NotificationRecord)
            .where(
                NotificationRecord.application_id == app_id,
                NotificationRecord.status == NotificationStatus.FAILED,
                NotificationRecord.created_at >= since,
            )
        )

        if channel is not None:
            stmt = stmt.where(NotificationRecord.channel == channel)

        result = await self.session.execute(stmt)
        return result.scalar_one()

    # ── Status Transitions ─────────────────────────────────────────────────

    async def update_status(
        self,
        instance: NotificationRecord,
        status: NotificationStatus,
        *,
        provider_message_id: str | None = None,
        error_message: str | None = None,
    ) -> NotificationRecord:
        """
        Transition a notification record to its final status.

        Called once per record after the dispatch provider responds.
        Records are never updated beyond this single transition —
        no further calls to this method after SENT, DELIVERED, or FAILED.

        Sets sent_at timestamp when status is SENT or DELIVERED.
        The service layer ensures this is only called on PENDING records.
        The repository does not validate the transition — the service does.

        Keyword-only arguments after status prevent positional mistakes
        on a function where all three params are strings:
            await repo.update_status(record, NotificationStatus.SENT,
                                     provider_message_id="msg-xyz")
            await repo.update_status(record, NotificationStatus.FAILED,
                                     error_message="Connection refused")
        """
        fields: dict = {"status": status}

        if provider_message_id is not None:
            fields["provider_message_id"] = provider_message_id

        if error_message is not None:
            fields["error_message"] = error_message

        # Record dispatch timestamp on any non-failure terminal state
        if status in (
            NotificationStatus.SENT,
            NotificationStatus.DELIVERED,
        ):
            from app.utils.time_utils import utcnow
            fields["sent_at"] = utcnow()

        return await self.update(instance, **fields)