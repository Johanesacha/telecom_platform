"""
NumberVerification domain model.

One record per MSISDN verification request.
Stores the classification result: valid/invalid, operator, line type.
Results are cached in Redis for 5 minutes — the DB record is the
long-term audit trail beyond the Redis TTL.
"""
from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import Boolean, Enum, Index, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.domain.mixins import TimestampMixin


class OperatorEnum(StrEnum):
    ORANGE   = "ORANGE"
    FREE     = "FREE"
    EXPRESSO = "EXPRESSO"
    UNKNOWN  = "UNKNOWN"


class LineType(StrEnum):
    MOBILE  = "MOBILE"
    FIXED   = "FIXED"
    VOIP    = "VOIP"
    UNKNOWN = "UNKNOWN"


class NumberVerification(Base, TimestampMixin):
    __tablename__ = "number_verifications"
    __table_args__ = (
        # Analytics: operator distribution per application
        Index(
            "ix_number_app_operator",
            "application_id",
            "operator",
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
    # Raw input from client — before normalisation
    raw_input: Mapped[str] = mapped_column(String(30), nullable=False)
    # E.164 normalised form — None if number is invalid
    msisdn_e164: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country_hint: Mapped[str] = mapped_column(
        String(2), default="SN", nullable=False  # ISO 3166-1 alpha-2
    )
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Simulated liveness — True for valid numbers with known operator
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    operator: Mapped[OperatorEnum] = mapped_column(
        Enum(OperatorEnum),
        default=OperatorEnum.UNKNOWN,
        nullable=False,
    )
    line_type: Mapped[LineType] = mapped_column(
        Enum(LineType),
        default=LineType.UNKNOWN,
        nullable=False,
    )
    country_code: Mapped[str | None] = mapped_column(
        String(10), nullable=True  # e.g. "+221" (dial prefix)
    )
    # Human-readable national format — e.g. "77 123 45 67"
    national_format: Mapped[str | None] = mapped_column(
        String(30), nullable=True
    )
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    is_sandbox: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )