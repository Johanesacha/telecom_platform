"""
ApiKey repository.

Responsible for all database access related to API keys.
The hot path method is get_active_by_prefix() — it fires on every
authenticated request and must be fast and correct.

Security contract:
- This repository does NOT perform hash comparison (that is security layer).
- This repository does NOT check token expiration (that is auth dependency).
- This repository filters on is_revoked and application.is_active only.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.domain.api_key import ApiKey
from app.domain.application import ClientApplication
from app.repositories.base import BaseRepository


class ApiKeyRepository(BaseRepository[ApiKey]):

    def __init__(self, session) -> None:
        super().__init__(ApiKey, session)

    async def get_by_prefix(self, prefix: str) -> ApiKey | None:
        stmt = (
            select(ApiKey)
            .where(ApiKey.key_prefix == prefix)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active_by_prefix(self, prefix: str) -> ApiKey | None:
        stmt = (
            select(ApiKey)
            .join(ApiKey.application)
            .where(
                ApiKey.key_prefix == prefix,
                ApiKey.is_revoked.is_(False),
                ClientApplication.is_active.is_(True),
            )
            .options(
                selectinload(ApiKey.application)
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_application_id(self, app_id: UUID) -> list[ApiKey]:
        stmt = (
            select(ApiKey)
            .where(ApiKey.application_id == app_id)
            .order_by(ApiKey.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_active_by_application_id(self, app_id: UUID) -> list[ApiKey]:
        from app.utils.time_utils import utcnow

        now = utcnow()
        stmt = (
            select(ApiKey)
            .where(
                ApiKey.application_id == app_id,
                ApiKey.is_revoked.is_(False),
            )
            .where(
                (ApiKey.expires_at.is_(None)) | (ApiKey.expires_at > now)
            )
            .order_by(ApiKey.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def revoke_key(self, instance: ApiKey) -> ApiKey:
        return await self.update(instance, is_revoked=True)

    async def update_last_used(self, instance: ApiKey) -> None:
        from app.utils.time_utils import utcnow

        await self.update(instance, last_used_at=utcnow().replace(tzinfo=None))

