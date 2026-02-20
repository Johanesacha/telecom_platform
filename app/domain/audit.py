from __future__ import annotations
import uuid
from datetime import datetime
from sqlalchemy import BigInteger, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base

class ApiCallLog(Base):
    """
    Append-only log of every API request.
    BigInteger PK (not UUID) for maximum insert performance.
    Composite indexes for dashboard queries.
    """
    __tablename__ = "api_call_logs"
    __table_args__ = (
        Index("ix_call_app_created", "application_id", "created_at"),
        Index("ix_call_service_status", "service_type", "status_code", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    application_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    endpoint: Mapped[str] = mapped_column(String(200), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_time_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    service_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    is_sandbox: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)