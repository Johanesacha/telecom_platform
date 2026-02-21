"""
User repository — human operators only (Managers and Admins).
Developers authenticate via ApiKey, not User.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select

from app.domain.user import User, UserRole
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):

    def __init__(self, session) -> None:
        super().__init__(User, session)

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email == email.lower().strip())
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active_by_email(self, email: str) -> User | None:
        stmt = (
            select(User)
            .where(User.email == email.lower().strip(), User.is_active.is_(True))
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_role(self, role: UserRole) -> list[User]:
        stmt = (
            select(User)
            .where(User.role == role, User.is_active.is_(True))
            .order_by(User.email)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def email_exists(self, email: str) -> bool:
        return await self.exists_by_field(User.email, email.lower().strip())

    async def store_refresh_token_hash(self, instance: User, token_hash: str) -> User:
        return await self.update(instance, refresh_token_hash=token_hash)

    async def rotate_refresh_token(self, instance: User, new_token_hash: str) -> User:
        return await self.update(instance, refresh_token_hash=new_token_hash)

    async def clear_refresh_token(self, instance: User) -> User:
        return await self.update(instance, refresh_token_hash=None)

    async def deactivate(self, instance: User) -> User:
        return await self.update(instance, is_active=False)

    async def change_role(self, instance: User, new_role: UserRole) -> User:
        return await self.update(instance, role=new_role)

    async def exists_by_field(self, column, value) -> bool:
        stmt = select(func.count()).select_from(User).where(column == value)
        result = await self.session.execute(stmt)
        return result.scalar_one() > 0