"""
Alembic migration environment — async configuration.
Runs SQLAlchemy metadata detection and applies migrations via asyncio.
"""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.database import Base  # noqa: F401
from app.domain.user import User  # noqa: F401
from app.domain.application import ClientApplication  # noqa: F401
from app.domain.api_key import ApiKey  # noqa: F401
from app.domain.sms import SMSMessage  # noqa: F401
from app.domain.ussd import USSDSession  # noqa: F401
from app.domain.payment import PaymentTransaction  # noqa: F401
from app.domain.audit import ApiCallLog  # noqa: F401
from app.core.config import settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode — generates SQL without a live connection.
    """
    url = settings.database_url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode with a live async engine.
    """
    connectable = create_async_engine(settings.database_url)

    async with connectable.connect() as connection:
        await connection.run_sync(
            lambda conn: context.configure(
                connection=conn,
                target_metadata=target_metadata,
                compare_type=True,
            )
        )
        await connection.run_sync(lambda conn: context.run_migrations())
        await connection.commit()
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())