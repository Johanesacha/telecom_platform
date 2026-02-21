"""
ClientApplication repository.

Serves registration, management UI, billing, and quota admin.
NOT on the authentication hot path — ApiKeyRepository handles that.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.domain.application import AppPlan, ClientApplication
from app.repositories.base import BaseRepository


class ApplicationRepository(BaseRepository[ClientApplication]):

    def __init__(self, session) -> None:
        super().__init__(ClientApplication, session)

    async def get_by_owner_email(self, email: str) -> ClientApplication | None:
        stmt = (
            select(ClientApplication)
            .where(ClientApplication.owner_email == email.lower().strip())
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_with_keys(self, app_id: UUID) -> ClientApplication | None:
        stmt = (
            select(ClientApplication)
            .where(ClientApplication.id == app_id)
            .options(selectinload(ClientApplication.api_keys))
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_active(self, *, skip: int = 0, limit: int = 50) -> list[ClientApplication]:
        stmt = (
            select(ClientApplication)
            .where(ClientApplication.is_active.is_(True))
            .order_by(ClientApplication.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_by_plan(self, plan: AppPlan, *, skip: int = 0, limit: int = 100) -> list[ClientApplication]:
        stmt = (
            select(ClientApplication)
            .where(
                ClientApplication.plan == plan,
                ClientApplication.is_active.is_(True),
            )
            .order_by(ClientApplication.owner_email)
            .offset(skip)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_ids(self, app_ids: list[UUID]) -> list[ClientApplication]:
        if not app_ids:
            return []
        stmt = (
            select(ClientApplication)
            .where(ClientApplication.id.in_(app_ids))
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_active(self) -> int:
        stmt = (
            select(func.count())
            .select_from(ClientApplication)
            .where(ClientApplication.is_active.is_(True))
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def deactivate(self, instance: ClientApplication) -> ClientApplication:
        return await self.update(instance, is_active=False)

    async def upgrade_plan(self, instance: ClientApplication, new_plan: AppPlan) -> ClientApplication:
        return await self.update(instance, plan=new_plan)