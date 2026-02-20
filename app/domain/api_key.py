from __future__ import annotations
import uuid
from datetime import datetime
from enum import StrEnum
from sqlalchemy import Boolean, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base
from app.domain.mixins import TimestampMixin


class KeyType(StrEnum):
    LIVE    = "LIVE"
    SANDBOX = "SANDBOX"


class ApiKey(Base, TimestampMixin):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("client_applications.id", ondelete="CASCADE"), nullable=False
    )
    key_prefix: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    key_type: Mapped[KeyType] = mapped_column(Enum(KeyType), default=KeyType.LIVE, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)

    application: Mapped["ClientApplication"] = relationship(  # type: ignore[name-defined]
        back_populates="api_keys"
    )