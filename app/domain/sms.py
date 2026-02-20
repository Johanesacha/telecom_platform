from __future__ import annotations
import uuid
from datetime import datetime
from enum import StrEnum
from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base
from app.domain.mixins import TimestampMixin


class SMSStatus(StrEnum):
    PENDING   = "PENDING"
    SENT      = "SENT"
    DELIVERED = "DELIVERED"
    FAILED    = "FAILED"


class SMSMessage(Base, TimestampMixin):
    __tablename__ = "sms_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("client_applications.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    to_number: Mapped[str] = mapped_column(String(20), nullable=False)
    from_alias: Mapped[str] = mapped_column(String(50), nullable=False, default="TelecomAPI")
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[SMSStatus] = mapped_column(
        Enum(SMSStatus), default=SMSStatus.PENDING, nullable=False, index=True
    )
    segment_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(nullable=True)
    is_sandbox: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    application: Mapped["ClientApplication"] = relationship()  # type: ignore[name-defined]