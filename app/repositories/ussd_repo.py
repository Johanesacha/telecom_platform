"""
USSDSession repository — PostgreSQL half of the dual-storage pattern.
This repository owns ONLY the PostgreSQL side. Never touches Redis.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update

from app.domain.ussd import USSDSession, USSDState
from app.repositories.base import BaseRepository


class USSDRepository(BaseRepository[USSDSession]):

    def __init__(self, session) -> None:
        super().__init__(USSDSession, session)

    async def get_by_session_id(self, session_id: str) -> USSDSession | None:
        stmt = select(USSDSession).where(USSDSession.session_id == session_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active_by_session_id(self, session_id: str) -> USSDSession | None:
        stmt = (
            select(USSDSession)
            .where(USSDSession.session_id == session_id, USSDSession.state == USSDState.ACTIVE)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_application(self, app_id: UUID, *, skip: int = 0, limit: int = 20, state_filter: USSDState | None = None) -> list[USSDSession]:
        stmt = (
            select(USSDSession)
            .where(USSDSession.application_id == app_id)
            .order_by(USSDSession.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        if state_filter is not None:
            stmt = stmt.where(USSDSession.state == state_filter)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_by_application(self, app_id: UUID, *, state_filter: USSDState | None = None) -> int:
        stmt = select(func.count()).select_from(USSDSession).where(USSDSession.application_id == app_id)
        if state_filter is not None:
            stmt = stmt.where(USSDSession.state == state_filter)
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def count_by_state(self, app_id: UUID) -> dict[str, int]:
        stmt = (
            select(USSDSession.state, func.count().label("total"))
            .where(USSDSession.application_id == app_id)
            .group_by(USSDSession.state)
        )
        result = await self.session.execute(stmt)
        return {row.state: row.total for row in result.all()}

    async def get_expired_active_sessions(self, reference_time: datetime, *, batch_size: int = 100) -> list[USSDSession]:
        stmt = (
            select(USSDSession)
            .where(USSDSession.state == USSDState.ACTIVE, USSDSession.expires_at < reference_time)
            .limit(batch_size)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def advance_step(self, instance: USSDSession, *, next_step: str, session_data: dict, new_expires_at: datetime) -> USSDSession:
        return await self.update(instance, current_step=next_step, session_data=session_data, expires_at=new_expires_at)

    async def mark_ended(self, instance: USSDSession) -> USSDSession:
        return await self.update(instance, state=USSDState.ENDED)

    async def mark_timed_out(self, instance: USSDSession) -> USSDSession:
        return await self.update(instance, state=USSDState.TIMEOUT)

    async def bulk_mark_timed_out(self, session_ids: list) -> int:
        if not session_ids:
            return 0
        stmt = (
            update(USSDSession)
            .where(USSDSession.id.in_(session_ids))
            .values(state=USSDState.TIMEOUT)
            .execution_options(synchronize_session="fetch")
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount