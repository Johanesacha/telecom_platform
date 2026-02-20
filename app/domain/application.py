from __future__ import annotations
import uuid
from enum import StrEnum
from sqlalchemy import Boolean, Enum, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base
from app.domain.mixins import TimestampMixin


class AppPlan(StrEnum):
    FREE     = "FREE"
    STANDARD = "STANDARD"
    PREMIUM  = "PREMIUM"


class ClientApplication(Base, TimestampMixin):
    __tablename__ = "client_applications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    owner_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    plan: Mapped[AppPlan] = mapped_column(Enum(AppPlan), default=AppPlan.FREE, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    api_keys: Mapped[list["ApiKey"]] = relationship(  # type: ignore[name-defined]
        back_populates="application", cascade="all, delete-orphan"
    )