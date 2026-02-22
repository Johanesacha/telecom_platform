"""
Notification dispatch request and response schemas.

Cross-field validation strategy:
  @field_validator on individual fields handles channel-independent checks
  (non-empty, length bounds, type checks).

  @model_validator(mode='after') handles channel-dependent checks:
    - SMS channel:   recipient must be E.164 phone number
    - EMAIL channel: recipient must be a valid email address
    - PUSH channel:  recipient is an opaque device token (not validated)
    - subject field: only meaningful for EMAIL — warned if provided for others

This split is required because @field_validator runs on the field value
alone (sibling fields not yet available). @model_validator(mode='after')
runs on the fully constructed model instance and can read all fields.

SMS channel quota exemption:
  NotificationService does not charge the notifications quota for SMS
  channel dispatches — SMS notifications are internal platform operations.
  This is a service-layer rule, not a schema rule. The schema does not
  express it — it accepts SMS channel requests normally.

PUSH channel recipient:
  Device tokens from FCM (Firebase Cloud Messaging) are ~160 character
  alphanumeric strings. APNs tokens are 64-character hex strings.
  No format validation is applied — both formats are accepted, and the
  platform placeholder returns FAILED with an explanatory message anyway.
"""
from __future__ import annotations

import re

from pydantic import (
    BaseModel, ConfigDict, EmailStr, Field,
    field_validator, model_validator,
)

from app.schemas.common import ApiMeta, PaginationMeta
from app.core.exceptions import InvalidMSISDNError

# Valid notification channels (matches NotificationChannel enum)
_VALID_CHANNELS = {"SMS", "EMAIL", "PUSH"}

# Basic device token bounds — not format-validated (platform-dependent)
_PUSH_TOKEN_MAX_LENGTH = 256

# SMS recipient: E.164 is validated via normalise_e164
# Email recipient: validated via pydantic EmailStr logic in model_validator

# Notification body limits
_BODY_MAX_CHARS = 4096   # generous — SMS body is further limited by carrier
_BODY_MIN_CHARS = 1
_SUBJECT_MAX_CHARS = 255


# ── Request Schemas ────────────────────────────────────────────────────────

class NotificationDispatchRequest(BaseModel):
    """
    Request body for POST /notifications/send.

    channel:   'SMS', 'EMAIL', or 'PUSH'.
               Determines how recipient is validated and how dispatch occurs.

    recipient: format depends on channel:
               SMS   → E.164 phone number (validated, normalised)
               EMAIL → valid email address (validated)
               PUSH  → device token (not validated — platform-specific format)

    subject:   meaningful only for EMAIL channel.
               For SMS and PUSH, providing a subject produces a validation
               warning (accepted but logged) — or a strict rejection depending
               on business requirements. We reject to prevent client confusion.

    body:      notification content. For SMS, carrier limits apply downstream.
               The platform does not enforce SMS segment limits here — the
               notification service handles truncation or rejection.

    idempotency_key: same 24-hour deduplication as SMS and payments.
    """
    model_config = ConfigDict(str_strip_whitespace=True)

    channel: str = Field(
        description="Delivery channel: SMS, EMAIL, or PUSH"
    )
    recipient: str = Field(
        description=(
            "Recipient address — format depends on channel: "
            "SMS: E.164 phone number (+221771234567), "
            "EMAIL: email address (user@example.com), "
            "PUSH: device token (FCM or APNs format)"
        )
    )
    body: str = Field(
        description="Notification content"
    )
    subject: str | None = Field(
        default=None,
        max_length=_SUBJECT_MAX_CHARS,
        description="Subject line (EMAIL channel only — ignored for SMS and PUSH)",
    )
    idempotency_key: str | None = Field(
        default=None,
        max_length=128,
        description="Client key for safe retries — same response within 24 hours",
    )

    @field_validator("channel")
    @classmethod
    def validate_channel(cls, v: str) -> str:
        normalised = v.upper().strip()
        if normalised not in _VALID_CHANNELS:
            raise ValueError(
                f"channel must be one of: {sorted(_VALID_CHANNELS)}. "
                f"Got: '{v}'"
            )
        return normalised

    @field_validator("body")
    @classmethod
    def validate_body(cls, v: str) -> str:
        if len(v) < _BODY_MIN_CHARS:
            raise ValueError("body cannot be empty")
        if len(v) > _BODY_MAX_CHARS:
            raise ValueError(
                f"body is {len(v)} characters. Maximum is {_BODY_MAX_CHARS}."
            )
        return v

    @field_validator("recipient")
    @classmethod
    def validate_recipient_non_empty(cls, v: str) -> str:
        """
        Channel-independent recipient check: non-empty and bounded length.

        Channel-specific format validation happens in @model_validator
        where the channel field is also available.
        """
        if not v:
            raise ValueError("recipient cannot be empty")
        if len(v) > _PUSH_TOKEN_MAX_LENGTH:
            raise ValueError(
                f"recipient is {len(v)} characters. "
                f"Maximum is {_PUSH_TOKEN_MAX_LENGTH}."
            )
        return v

    @model_validator(mode="after")
    def validate_channel_specific_fields(self) -> "NotificationDispatchRequest":
        """
        Channel-dependent cross-field validation.

        Runs after all field validators — self.channel and self.recipient
        are both available here.

        SMS:   normalise recipient to E.164 via normalise_e164()
        EMAIL: validate recipient as email address
        PUSH:  no format validation — accept opaque device token

        Also validates subject: reject if provided for non-EMAIL channels.
        """
        channel = self.channel
        recipient = self.recipient

        if channel == "SMS":
            # Normalise to E.164 — raises ValueError on invalid number
            from app.utils.msisdn import normalise_e164
            try:
                self.recipient = normalise_e164(recipient, country_hint="SN")
            except InvalidMSISDNError as exc:
                raise ValueError(
                    f"recipient is not a valid phone number for SMS channel: {exc}"
                ) from exc
            except Exception as exc:
                raise ValueError(
                    f"recipient validation failed for SMS channel: {exc}"
                ) from exc

        elif channel == "EMAIL":
            # Validate email format using regex consistent with common standards.
            # We avoid importing EmailStr directly to validate a runtime value —
            # instead use a well-tested pattern covering the vast majority of
            # real-world email addresses without false positives.
            email_pattern = re.compile(
                r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
            )
            if not email_pattern.match(recipient):
                raise ValueError(
                    f"recipient '{recipient}' is not a valid email address "
                    "for EMAIL channel"
                )

        elif channel == "PUSH":
            # PUSH recipient is an opaque device token.
            # No format validation — FCM and APNs tokens differ in format.
            # Length check already performed in @field_validator.
            pass

        # Subject validation: reject for non-EMAIL channels
        if self.subject is not None and channel != "EMAIL":
            raise ValueError(
                f"subject is only valid for EMAIL channel. "
                f"Got subject with channel='{channel}'. "
                "Remove subject or change channel to EMAIL."
            )

        return self


# ── Response Schemas ───────────────────────────────────────────────────────

class NotificationResponse(BaseModel):
    """
    Serialised notification record.

    Returned by POST /notifications/send (202 or 200 on idempotency hit)
    and GET /notifications/{id}.

    status progression:
      PENDING → SENT      (provider acknowledged)
              → FAILED    (provider rejected or unreachable)
      SENT    → DELIVERED (provider confirmed delivery — if provider supports it)

    provider_message_id: assigned by the SMS/email provider on acceptance.
                         None until the provider responds.
                         Useful for debugging delivery issues with the provider.
    """
    model_config = ConfigDict(from_attributes=True)

    id: str
    channel: str
    recipient: str
    status: str
    subject: str | None
    provider_message_id: str | None
    is_sandbox: bool
    created_at: str

    @classmethod
    def from_orm(cls, record) -> "NotificationResponse":
        """Build from NotificationRecord ORM instance."""
        return cls(
            id=str(record.id),
            channel=record.channel.value
                if hasattr(record.channel, "value") else str(record.channel),
            recipient=record.recipient,
            status=record.status.value
                if hasattr(record.status, "value") else str(record.status),
            subject=getattr(record, "subject", None),
            provider_message_id=getattr(record, "provider_message_id", None),
            is_sandbox=record.is_sandbox,
            created_at=record.created_at.isoformat() if record.created_at else "",
        )

    @classmethod
    def from_cache(cls, cached: dict) -> "NotificationResponse":
        """Build from idempotency cache dict returned by NotificationService."""
        return cls(
            id=cached["id"],
            channel=cached["channel"],
            recipient=cached["recipient"],
            status=cached["status"],
            subject=cached.get("subject"),
            provider_message_id=cached.get("provider_message_id"),
            is_sandbox=cached["is_sandbox"],
            created_at=cached.get("created_at", ""),
        )


class NotificationHistoryResponse(BaseModel):
    """
    Paginated notification history for GET /notifications/history.
    """
    model_config = ConfigDict(frozen=True)

    success: bool = True
    items: list[NotificationResponse]
    pagination: PaginationMeta
    meta: ApiMeta

    @classmethod
    def from_service(
        cls,
        items: list,
        *,
        paginated,
        request_id: str,
    ) -> "NotificationHistoryResponse":
        return cls(
            items=[NotificationResponse.from_orm(r) for r in items],
            pagination=PaginationMeta.from_paginated_result(paginated),
            meta=ApiMeta.build(request_id),
        )