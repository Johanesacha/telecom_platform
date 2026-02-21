"""
NotificationRecord domain model.

One record per notification dispatch attempt.
Channels: SMS, EMAIL, PUSH.
Created in PENDING state, updated once to SENT/FAILED/DELIVERED.
Terminal after first status update — no further modifications.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, Enum, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.domain.mixins import TimestampMixin


class NotificationChannel(StrEnum):
    SMS   = "SMS"
    EMAIL = "EMAIL"
    PUSH  = "PUSH"


class NotificationStatus(StrEnum):
    PENDING   = "PENDING"
    SENT      = "SENT"
    DELIVERED = "DELIVERED"
    FAILED    = "FAILED"


class NotificationRecord(Base, TimestampMixin):
    __tablename__ = "notification_records"
    __table_args__ = (
        # Dashboard queries: per-application by channel and status
        Index(
            "ix_notification_app_channel_status",
            "application_id",
            "channel",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("client_applications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Channel determines dispatch path in NotificationService
    channel: Mapped[NotificationChannel] = mapped_column(
        Enum(NotificationChannel), nullable=False
    )
    # Recipient identifier — format depends on channel:
    #   SMS   → E.164 phone number (+221771234567)
    #   EMAIL → email address (user@example.com)
    #   PUSH  → device token (platform-specific opaque string)
    recipient: Mapped[str] = mapped_column(String(512), nullable=False)

    subject: Mapped[str | None] = mapped_column(
        String(255), nullable=True  # used for EMAIL subject lines
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[NotificationStatus] = mapped_column(
        Enum(NotificationStatus),
        default=NotificationStatus.PENDING,
        nullable=False,
        index=True,
    )
    # Idempotency key — same structure as SMS and payment
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128), unique=True, nullable=True
    )
    # request_id for end-to-end tracing
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Provider-level message ID for delivery tracking
    provider_message_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(nullable=True)
    is_sandbox: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )