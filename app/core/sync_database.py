"""
Synchronous SQLAlchemy engine for Celery tasks.
Celery workers run outside the async event loop — they need a sync engine.
This is a SEPARATE engine from the async one. Both point to the same DB.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings


sync_engine = create_engine(
    settings.sync_database_url,
    pool_size=5,
    max_overflow=5,
    pool_pre_ping=True,
)

SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    autoflush=False,
    autocommit=False,
)


def get_sync_db() -> Session:
    """
    Context manager for sync DB sessions in Celery tasks.
    Usage inside a task:
        with get_sync_db() as session:
            record = session.get(SMSMessage, message_id)
    """
    return SyncSessionLocal()