"""
Generic async base repository for SQLAlchemy 2.0.

All domain repositories inherit from this class.
This layer is responsible for one thing: translating between
the service layer and SQLAlchemy. It contains no business logic.

Rules:
- Never commit inside a repository method. Only flush.
- Always refresh after write operations to get server-generated values.
- Return None on not found — never raise ResourceNotFoundError here.
  That is the service layer's responsibility.
- Use SQLAlchemy 2.0 select() syntax — never session.query() (legacy).
"""
from __future__ import annotations

from typing import Any, Generic, TypeVar
from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import Base

ModelType = TypeVar("ModelType", bound=Base)


class BaseRepository(Generic[ModelType]):

    def __init__(
        self,
        model: type[ModelType],
        session: AsyncSession,
    ) -> None:
        self.model = model
        self.session = session

    async def get_by_id(self, record_id: UUID | int) -> ModelType | None:
        return await self.session.get(self.model, record_id)

    async def list(self, *, skip: int = 0, limit: int = 20) -> list[ModelType]:
        stmt = select(self.model).offset(skip).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count(self) -> int:
        stmt = select(func.count()).select_from(self.model)
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def create(self, **kwargs: Any) -> ModelType:
        instance = self.model(**kwargs)
        self.session.add(instance)
        await self.session.flush()
        await self.session.refresh(instance)
        return instance

    async def update(self, instance: ModelType, **kwargs: Any) -> ModelType:
        for field, value in kwargs.items():
            if not hasattr(instance, field):
                raise AttributeError(
                    f"{self.model.__name__} has no attribute '{field}'. "
                    "Check for typos in the field name passed to update()."
                )
            setattr(instance, field, value)
        self.session.add(instance)
        await self.session.flush()
        await self.session.refresh(instance)
        return instance

    async def delete(self, instance: ModelType) -> None:
        await self.session.delete(instance)
        await self.session.flush()

    async def exists(self, record_id: UUID | int) -> bool:
        pk_col = self.model.__mapper__.primary_key[0]
        stmt = select(func.count()).select_from(self.model).where(
            pk_col == record_id
        )
        result = await self.session.execute(stmt)
        return result.scalar_one() > 0