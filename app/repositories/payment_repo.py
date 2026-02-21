"""
PaymentTransaction repository.
Two deduplication mechanisms: reference (business) + idempotency_key (transport).
Strict Decimal discipline — no float, no Python arithmetic on amounts.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select

from app.domain.payment import PaymentStatus, PaymentTransaction
from app.repositories.base import BaseRepository


class PaymentRepository(BaseRepository[PaymentTransaction]):

    def __init__(self, session) -> None:
        super().__init__(PaymentTransaction, session)

    async def get_by_reference(self, app_id: UUID, reference: str) -> PaymentTransaction | None:
        stmt = (
            select(PaymentTransaction)
            .where(
                PaymentTransaction.application_id == app_id,
                PaymentTransaction.reference == reference,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_idempotency_key(self, app_id: UUID, idempotency_key: str) -> PaymentTransaction | None:
        stmt = (
            select(PaymentTransaction)
            .where(
                PaymentTransaction.application_id == app_id,
                PaymentTransaction.idempotency_key == idempotency_key,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id_for_application(self, transaction_id: UUID, app_id: UUID) -> PaymentTransaction | None:
        stmt = (
            select(PaymentTransaction)
            .where(
                PaymentTransaction.id == transaction_id,
                PaymentTransaction.application_id == app_id,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_application(self, app_id: UUID, *, skip: int = 0, limit: int = 20, status_filter: PaymentStatus | None = None) -> list[PaymentTransaction]:
        stmt = (
            select(PaymentTransaction)
            .where(PaymentTransaction.application_id == app_id)
            .order_by(PaymentTransaction.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        if status_filter is not None:
            stmt = stmt.where(PaymentTransaction.status == status_filter)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_by_application(self, app_id: UUID, *, status_filter: PaymentStatus | None = None) -> int:
        stmt = (
            select(func.count())
            .select_from(PaymentTransaction)
            .where(PaymentTransaction.application_id == app_id)
        )
        if status_filter is not None:
            stmt = stmt.where(PaymentTransaction.status == status_filter)
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def sum_completed_amount(self, app_id: UUID, *, since: datetime | None = None) -> Decimal:
        stmt = (
            select(func.sum(PaymentTransaction.amount))
            .where(
                PaymentTransaction.application_id == app_id,
                PaymentTransaction.status == PaymentStatus.COMPLETED,
            )
        )
        if since is not None:
            stmt = stmt.where(PaymentTransaction.created_at >= since)
        result = await self.session.execute(stmt)
        raw = result.scalar_one()
        return Decimal(str(raw)) if raw is not None else Decimal("0")

    async def count_by_status(self, app_id: UUID) -> dict[str, int]:
        stmt = (
            select(PaymentTransaction.status, func.count().label("total"))
            .where(PaymentTransaction.application_id == app_id)
            .group_by(PaymentTransaction.status)
        )
        result = await self.session.execute(stmt)
        return {row.status: row.total for row in result.all()}

    async def average_amount_by_operator(self, app_id: UUID, *, since: datetime | None = None) -> dict[str, Decimal]:
        stmt = (
            select(PaymentTransaction.operator, func.avg(PaymentTransaction.amount).label("avg_amount"))
            .where(
                PaymentTransaction.application_id == app_id,
                PaymentTransaction.status == PaymentStatus.COMPLETED,
                PaymentTransaction.operator.is_not(None),
            )
            .group_by(PaymentTransaction.operator)
        )
        if since is not None:
            stmt = stmt.where(PaymentTransaction.created_at >= since)
        result = await self.session.execute(stmt)
        return {row.operator: Decimal(str(row.avg_amount)) for row in result.all() if row.avg_amount is not None}

    async def update_status(self, instance: PaymentTransaction, status: PaymentStatus) -> PaymentTransaction:
        fields: dict = {"status": status}
        terminal_states = {PaymentStatus.COMPLETED, PaymentStatus.FAILED, PaymentStatus.REVERSED}
        if status in terminal_states:
            from app.utils.time_utils import utcnow
            fields["completed_at"] = utcnow()
        return await self.update(instance, **fields)