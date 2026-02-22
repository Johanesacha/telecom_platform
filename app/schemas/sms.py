"""
SMS request and response schemas.

Validation chain for SMSSendRequest.to_number:
  1. @field_validator calls normalise_e164() from app/utils/msisdn.py
  2. normalise_e164() calls parse_msisdn() which calls phonenumbers.parse()
  3. InvalidMSISDNError from parse_msisdn() is caught here and re-raised
     as ValueError — Pydantic converts it to a 422 validation error with
     field='to_number' in the ErrorDetail.
  4. Stored value is always E.164 (+221771234567), never raw input.

message_text ceiling of 1224 characters:
  _MAX_SEGMENTS (8) × _GSM7_SEGMENT_MAX (153) = 1224.
  Schema enforces the character ceiling without importing service-layer
  segment logic. The service still calculates actual segments (which
  depend on encoding) — the schema only rejects the obviously-impossible.

SMSHistoryResponse:
  Convenience wrapper around PaginatedResponse[SMSStatusResponse].
  Route handlers call SMSHistoryResponse.from_service() rather than
  building the nested structure manually — keeps routes thin.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import ApiMeta, PaginatedResponse, PaginationMeta
from app.utils.msisdn import normalise_e164
from app.core.exceptions import InvalidMSISDNError

# Character ceiling for SMS messages:
# 8 segments × 153 GSM-7 chars per multipart segment = 1224
_SMS_MAX_CHARS: int = 1224
_SMS_MIN_CHARS: int = 1


# ── Request Schemas ────────────────────────────────────────────────────────

class SMSSendRequest(BaseModel):
    """
    Request body for POST /sms/send.

    to_number: validated and normalised to E.164 by field_validator.
               The stored value is always E.164 — never raw input.
               Accepts: '77 123 45 67', '+221771234567', '00221771234567'.
               Rejects: invalid numbers, empty strings, obviously wrong formats.

    message_text: raw message content. Segment count is calculated by
                  SMSService.send() using the full GSM-7/UCS-2 encoding logic.
                  Schema enforces only the absolute character ceiling.

    from_alias: optional sender ID (e.g. 'TELECOM'). Subject to carrier
                restrictions — max 11 alphanumeric characters for most carriers.
                Platform passes through without validation; carrier rejects invalid ones.

    idempotency_key: client-provided key for safe retries. If present and a
                     previous request with this key succeeded, the cached
                     response is returned immediately without re-sending.
    """
    model_config = ConfigDict(str_strip_whitespace=True)

    to_number: str = Field(
        description=(
            "Recipient phone number. Accepts any reasonable format — "
            "normalised to E.164 before storage. "
            "Examples: '77 123 45 67', '+221771234567', '00221771234567'"
        )
    )
    message_text: str = Field(
        description=(
            f"SMS message content. "
            f"GSM-7: up to {_SMS_MAX_CHARS} characters ({_SMS_MAX_CHARS // 153} segments × 153). "
            f"UCS-2 (any non-GSM character): up to 536 characters (8 × 67)."
        )
    )
    from_alias: str | None = Field(
        default=None,
        max_length=11,
        description="Optional sender ID. Max 11 alphanumeric characters.",
    )
    idempotency_key: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Client-generated unique key for safe retries. "
            "Same key within 24 hours returns the original response without re-sending."
        ),
    )

    @field_validator("to_number")
    @classmethod
    def validate_and_normalise_to_number(cls, v: str) -> str:
        """
        Validate and normalise the recipient phone number to E.164.

        Calls normalise_e164() which invokes the phonenumbers library.
        InvalidMSISDNError from the library is converted to ValueError
        so Pydantic produces a 422 with field='to_number'.

        After this validator, the value stored on the model is always
        a valid E.164 string — SMSService.send() never receives raw input.
        """
        try:
            return normalise_e164(v, country_hint="SN")
        except InvalidMSISDNError as exc:
            raise ValueError(str(exc)) from exc
        except Exception as exc:
            raise ValueError(
                f"Phone number '{v}' could not be validated: {exc}"
            ) from exc

    @field_validator("message_text")
    @classmethod
    def validate_message_text(cls, v: str) -> str:
        """
        Validate message content length.

        Minimum 1 character — empty messages are rejected.
        Maximum 1224 characters — absolute ceiling for 8 GSM-7 segments.
        Note: actual segment count depends on encoding and is computed
        in SMSService.send() using the full GSM-7/UCS-2 algorithm.
        """
        if len(v) < _SMS_MIN_CHARS:
            raise ValueError("message_text cannot be empty")
        if len(v) > _SMS_MAX_CHARS:
            raise ValueError(
                f"message_text is {len(v)} characters. "
                f"Maximum is {_SMS_MAX_CHARS} characters "
                f"(8 segments × 153 GSM-7 characters)."
            )
        return v


# ── Response Schemas ───────────────────────────────────────────────────────

class SMSSendResponse(BaseModel):
    """
    Response for POST /sms/send (202 Accepted or 200 OK on idempotency hit).

    All identifiers and timestamps are strings:
      id:         UUID as str — avoids JSON platform ambiguity
      created_at: ISO 8601 UTC string — never naive datetime

    status is always PENDING on a genuine new send (202).
    On an idempotency hit (200), status reflects the current state of
    the original message — may be SENT, DELIVERED, or FAILED.

    segment_count: how many SMS segments the message requires.
    Billing is typically per-segment — clients should surface this to users.
    """
    model_config = ConfigDict(from_attributes=True)

    id: str
    to_number: str
    status: str
    segment_count: int
    is_sandbox: bool
    created_at: str

    @classmethod
    def from_orm(cls, message) -> "SMSSendResponse":
        """Build from SMSMessage ORM instance or idempotency cache dict."""
        if isinstance(message, dict):
            # Idempotency cache hit — already serialised
            return cls(**{k: message[k] for k in cls.model_fields if k in message})
        return cls(
            id=str(message.id),
            to_number=message.to_number,
            status=message.status.value if hasattr(message.status, "value") else str(message.status),
            segment_count=message.segment_count,
            is_sandbox=message.is_sandbox,
            created_at=message.created_at.isoformat() if message.created_at else "",
        )


class SMSStatusResponse(BaseModel):
    """
    Response for GET /sms/{id}/status and items in history list.

    Extends SMSSendResponse with delivery tracking fields:
      provider_message_id: ID assigned by the SMS provider (None until sent)
      updated_at: last status change timestamp
      from_alias: sender ID used for this message
    """
    model_config = ConfigDict(from_attributes=True)

    id: str
    to_number: str
    from_alias: str | None
    status: str
    segment_count: int
    provider_message_id: str | None
    is_sandbox: bool
    created_at: str
    updated_at: str | None

    @classmethod
    def from_orm(cls, message) -> "SMSStatusResponse":
        """Build from SMSMessage ORM instance."""
        return cls(
            id=str(message.id),
            to_number=message.to_number,
            from_alias=getattr(message, "from_alias", None),
            status=message.status.value if hasattr(message.status, "value") else str(message.status),
            segment_count=message.segment_count,
            provider_message_id=getattr(message, "provider_message_id", None),
            is_sandbox=message.is_sandbox,
            created_at=message.created_at.isoformat() if message.created_at else "",
            updated_at=message.updated_at.isoformat() if message.updated_at else None,
        )


class SMSHistoryResponse(BaseModel):
    """
    Paginated SMS history response for GET /sms/history.

    Wraps PaginatedResponse[SMSStatusResponse] with a convenience
    factory so route handlers remain thin.

    Usage in route handler:
        items, total = await sms_svc.list_history(skip=skip, limit=limit)
        paginated = paginate(items, total, params)
        return SMSHistoryResponse.from_service(
            items=items,
            paginated=paginated,
            request_id=request.state.request_id,
        )
    """
    model_config = ConfigDict(frozen=True)

    success: bool = True
    items: list[SMSStatusResponse]
    pagination: PaginationMeta
    meta: ApiMeta

    @classmethod
    def from_service(
        cls,
        items: list,
        *,
        paginated,
        request_id: str,
    ) -> "SMSHistoryResponse":
        """
        Build from list of SMSMessage ORM instances and PaginatedResult.

        Args:
            items:      List of SMSMessage ORM objects from SMSService.list_history()
            paginated:  PaginatedResult from app/utils/pagination.paginate()
            request_id: From request.state.request_id
        """
        return cls(
            items=[SMSStatusResponse.from_orm(m) for m in items],
            pagination=PaginationMeta.from_paginated_result(paginated),
            meta=ApiMeta.build(request_id),
        )