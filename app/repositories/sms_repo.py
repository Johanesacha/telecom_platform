"""
SMSMessage repository.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select

from app.domain.sms import SMSMessage, SMSStatus
from app.repositories.base import BaseRepository


class SMSRepository(BaseRepository[SMSMessage]):

    def __init__(self, session) -> None:
        super().__init__(SMSMessage, session)

    async def get_by_idempotency_key(self, app_id: UUID, idempotency_key: str) -> SMSMessage | None:
        stmt = (
            select(SMSMessage)
            .where(
                SMSMessage.application_id == app_id,
                SMSMessage.idempotency_key == idempotency_key,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id_for_application(self, message_id: UUID, app_id: UUID) -> SMSMessage | None:
        stmt = (
            select(SMSMessage)
            .where(
                SMSMessage.id == message_id,
                SMSMessage.application_id == app_id,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_application(self, app_id: UUID, *, skip: int = 0, limit: int = 20, status_filter: SMSStatus | None = None) -> list[SMSMessage]:
        stmt = (
            select(SMSMessage)
            .where(SMSMessage.application_id == app_id)
            .order_by(SMSMessage.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        if status_filter is not None:
            stmt = stmt.where(SMSMessage.status == status_filter)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_by_application(self, app_id: UUID, *, status_filter: SMSStatus | None = None) -> int:
        stmt = (
            select(func.count())
            .select_from(SMSMessage)
            .where(SMSMessage.application_id == app_id)
        )
        if status_filter is not None:
            stmt = stmt.where(SMSMessage.status == status_filter)
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def count_by_status(self, app_id: UUID) -> dict[str, int]:
        stmt = (
            select(SMSMessage.status, func.count().label("total"))
            .where(SMSMessage.application_id == app_id)
            .group_by(SMSMessage.status)
        )
        result = await self.session.execute(stmt)
        return {row.status: row.total for row in result.all()}

    async def count_failed_since(self, app_id: UUID, since_timestamp) -> int:
        stmt = (
            select(func.count())
            .select_from(SMSMessage)
            .where(
                SMSMessage.application_id == app_id,
                SMSMessage.status == SMSStatus.FAILED,
                SMSMessage.created_at >= since_timestamp,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def update_status(self, instance: SMSMessage, status: SMSStatus, *, provider_message_id: str | None = None, error_code: str | None = None, error_message: str | None = None) -> SMSMessage:
        fields: dict = {"status": status}
        if provider_message_id is not None:
            fields["provider_message_id"] = provider_message_id
        if error_code is not None:
            fields["error_code"] = error_code
        if error_message is not None:
            fields["error_message"] = error_message
        if status == SMSStatus.DELIVERED:
            from app.utils.time_utils import utcnow
            fields["delivered_at"] = utcnow()
        return await self.update(instance, **fields)