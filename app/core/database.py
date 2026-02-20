"""
Async SQLAlchemy engine and session factory.
All FastAPI routes use AsyncSession via the get_db() dependency.
Celery tasks use synchronous sessions — see app/core/sync_database.py.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


engine = create_async_engine(
    settings.database_url,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
    echo=settings.debug,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """
    All SQLAlchemy models inherit from this Base.
    Import from here, not from sqlalchemy.orm directly.
    """
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency injected into every route that needs DB access.
    Usage: db: AsyncSession = Depends(get_db)
    Guarantees session is closed after request completes, even on error.
    """
    async with AsyncSessionLocal() as session:
        yield session