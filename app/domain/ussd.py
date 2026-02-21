from __future__ import annotations
import uuid
from datetime import datetime
from enum import StrEnum
from sqlalchemy import Boolean, Enum, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base
from app.domain.mixins import TimestampMixin


class USSDState(StrEnum):
    ACTIVE  = "ACTIVE"
    ENDED   = "ENDED"
    TIMEOUT = "TIMEOUT"


class USSDSession(Base, TimestampMixin):
    __tablename__ = "ussd_sessions"
    __table_args__ = (
        Index(
            "ix_ussd_app_state_created",
            "application_id",
            "state",
            "created_at",
        ),
        Index(
            "ix_ussd_state_expires",
            "state",
            "expires_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("client_applications.id"), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    msisdn: Mapped[str] = mapped_column(String(20), nullable=False)
    current_step: Mapped[str] = mapped_column(String(100), default="MAIN_MENU", nullable=False)
    session_data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    state: Mapped[USSDState] = mapped_column(Enum(USSDState), default=USSDState.ACTIVE, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    is_sandbox: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)