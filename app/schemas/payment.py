"""
Payment request and response schemas.

The Decimal type annotation on PaymentInitiateRequest.amount is the
single most important type decision in this schema file.

Why Decimal and not float:
  JSON numbers are IEEE 754 doubles. Pydantic with float annotation
  parses 5000.50 as 5000.5 (float), then Decimal(5000.5) produces
  Decimal('5000.500000000000045474735088646411895751953125').
  Pydantic with Decimal annotation parses 5000.50 as Decimal('5000.50')
  via string conversion internally — exact, no binary contamination.

Why amount is str in response schemas:
  JSON has no Decimal type. Serialising Decimal('5000.50') as a JSON
  number produces 5000.5 — the trailing zero is lost, which matters
  for display and for round-trip parsing. As a string '5000.50' is
  unambiguous. The API reference documents that amount is always a
  decimal string in responses.

reference validator:
  Payment references are permanent deduplication keys. They must be:
    - Non-empty after stripping whitespace
    - Bounded (max 128 chars — enough for any UUID-based scheme)
    - Safe for URLs and log files (alphanumeric + - _ / .)
  Leading/trailing whitespace is stripped before validation.
  Internal whitespace is rejected — 'ORD 001' is not a valid reference.
"""
from __future__ import annotations

import re
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.exceptions import InvalidAmountError, UnsupportedCurrencyError
from app.schemas.common import ApiMeta, PaginationMeta
from app.utils.money import validate_currency, validate_positive, from_any

# Reference format: alphanumeric + hyphen, underscore, slash, dot
_REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9\-_/.]{1,128}$")


# ── Request Schemas ────────────────────────────────────────────────────────

class PaymentInitiateRequest(BaseModel):
    """
    Request body for POST /payments/initiate.

    amount MUST be Decimal — never float, never bare str.
    Pydantic v2 with Decimal annotation correctly handles JSON numbers:
      5000.50  (JSON number) → Decimal('5000.50')  ✓
      "5000.50" (JSON string) → Decimal('5000.50')  ✓
      5000.5   (float coercion) → Decimal('5000.5') then quantize → Decimal('5000.50') ✓

    @field_validator on amount calls validate_positive() from money.py.
    @field_validator on currency calls validate_currency() from money.py.
    Both raise ValueError on failure — Pydantic produces 422 with field name.

    The service layer re-validates amount through _validate_and_prepare_amount()
    which calls from_any() → validate_positive() → quantize_amount().
    Dual validation is intentional — schema catches format errors early,
    service enforces the full pipeline before any DB write.
    """
    model_config = ConfigDict(str_strip_whitespace=True)

    payer_msisdn: str = Field(
        description="Payer phone number (any format — normalised to E.164 by service)"
    )
    receiver_msisdn: str = Field(
        description="Receiver phone number (any format — normalised to E.164 by service)"
    )
    amount: Decimal = Field(
        description=(
            "Transaction amount as a decimal number. "
            "Use a JSON number (5000.50) or decimal string ('5000.50'). "
            "Never use float — JSON floats lose precision for monetary values."
        )
    )
    currency: str = Field(
        description="ISO 4217 currency code. Supported: XOF, XAF, EUR, USD"
    )
    reference: str = Field(
        description=(
            "Client-assigned unique transaction reference (e.g. 'ORD-2026-00847'). "
            "Permanent deduplication key — a reference can only be used once per application. "
            "Alphanumeric, hyphens, underscores, slashes, dots. Max 128 characters."
        )
    )
    idempotency_key: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Client-generated key for safe HTTP retries (24-hour window). "
            "Distinct from reference: idempotency_key is transport-level, "
            "reference is business-level deduplication."
        ),
    )

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v: Decimal) -> Decimal:
        """
        Validate amount is strictly positive.

        Calls validate_positive() from money.py which raises InvalidAmountError.
        Caught here and re-raised as ValueError for Pydantic's 422 mechanism.

        Does NOT call quantize_amount() — that happens in the service layer.
        Schema validates constraint (> 0), service validates precision (2dp).
        """
        try:
            return validate_positive(v, field_name="amount")
        except InvalidAmountError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("currency")
    @classmethod
    def validate_currency_field(cls, v: str) -> str:
        """
        Validate currency against SUPPORTED_CURRENCIES from money.py.

        Returns the normalised uppercase currency code on success.
        Raises ValueError (→ 422) if the currency is not supported.
        """
        try:
            return validate_currency(v)
        except UnsupportedCurrencyError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("reference")
    @classmethod
    def validate_reference(cls, v: str) -> str:
        """
        Validate payment reference format.

        Strips leading/trailing whitespace (handled by str_strip_whitespace=True).
        Rejects empty references, internal whitespace, and unsafe characters.
        The character set (alphanumeric + - _ / .) is safe for URLs and log files.
        """
        if not v:
            raise ValueError("reference cannot be empty")
        if not _REFERENCE_PATTERN.match(v):
            raise ValueError(
                f"reference '{v}' contains invalid characters or exceeds 128 characters. "
                "Allowed: letters, digits, hyphens (-), underscores (_), "
                "slashes (/), and dots (.). No spaces."
            )
        return v

    @field_validator("payer_msisdn", "receiver_msisdn")
    @classmethod
    def validate_msisdn_non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("MSISDN cannot be empty")
        return v


# ── Response Schemas ───────────────────────────────────────────────────────

class PaymentStatusResponse(BaseModel):
    """
    Serialised payment transaction for status and history responses.

    amount is returned as str — see module docstring for rationale.
    Clients that need arithmetic should parse it to their Decimal type:
      Python: Decimal(response['amount'])
      JavaScript: use decimal.js or similar library

    operator: the detected mobile money operator for the payer MSISDN.
              One of: ORANGE, FREE, EXPRESSO, UNKNOWN.
              Used for operator-specific routing in production deployments.

    status progression:
      INITIATED → PENDING → COMPLETED
                           → FAILED
      COMPLETED → REVERSED (admin refund only)
    """
    model_config = ConfigDict(from_attributes=True)

    id: str
    reference: str
    status: str
    amount: str = Field(
        description="Transaction amount as decimal string (e.g. '5000.50'). Parse with Decimal(), not float()."
    )
    currency: str
    payer_msisdn: str
    receiver_msisdn: str
    operator: str | None
    is_sandbox: bool
    created_at: str
    updated_at: str | None
    completed_at: str | None

    @classmethod
    def from_orm(cls, tx) -> "PaymentStatusResponse":
        """
        Build from PaymentTransaction ORM instance.

        amount is converted to str via str(Decimal) which preserves
        trailing zeros: Decimal('5000.50') → '5000.50', not '5000.5'.
        """
        return cls(
            id=str(tx.id),
            reference=tx.reference,
            status=tx.status.value if hasattr(tx.status, "value") else str(tx.status),
            amount=str(tx.amount) if tx.amount is not None else "0.00",
            currency=tx.currency,
            payer_msisdn=tx.payer_msisdn,
            receiver_msisdn=tx.receiver_msisdn,
            operator=getattr(tx, "operator", None),
            is_sandbox=tx.is_sandbox,
            created_at=tx.created_at.isoformat() if tx.created_at else "",
            updated_at=tx.updated_at.isoformat() if tx.updated_at else None,
            completed_at=tx.completed_at.isoformat() if tx.completed_at else None,
        )


class PaymentInitiateResponse(BaseModel):
    """
    Response for POST /payments/initiate (202 Accepted or 200 on idempotency hit).

    Subset of PaymentStatusResponse — returns immediately after record creation.
    Full status including provider response is available via GET /payments/{id}.

    amount as str — same rationale as PaymentStatusResponse.
    """
    model_config = ConfigDict(frozen=True)

    id: str
    reference: str
    status: str
    amount: str
    currency: str
    is_sandbox: bool
    created_at: str
    message: str = Field(
        default=(
            "Payment initiated successfully. "
            "Use GET /payments/{id} to track status. "
            "Processing is asynchronous — final status may take up to 60 seconds."
        )
    )

    @classmethod
    def from_orm(cls, tx) -> "PaymentInitiateResponse":
        """Build from PaymentTransaction ORM instance or idempotency cache dict."""
        if isinstance(tx, dict):
            return cls(
                id=tx["id"],
                reference=tx["reference"],
                status=tx["status"],
                amount=tx["amount"],
                currency=tx["currency"],
                is_sandbox=tx["is_sandbox"],
                created_at=tx.get("created_at", ""),
            )
        return cls(
            id=str(tx.id),
            reference=tx.reference,
            status=tx.status.value if hasattr(tx.status, "value") else str(tx.status),
            amount=str(tx.amount) if tx.amount is not None else "0.00",
            currency=tx.currency,
            is_sandbox=tx.is_sandbox,
            created_at=tx.created_at.isoformat() if tx.created_at else "",
        )


class PaymentHistoryResponse(BaseModel):
    """
    Paginated payment history for GET /payments/history.

    Usage in route handler:
        items, total = await payment_svc.list_history(skip=skip, limit=limit)
        paginated = paginate(items, total, params)
        return PaymentHistoryResponse.from_service(
            items=items, paginated=paginated,
            request_id=request.state.request_id,
        )
    """
    model_config = ConfigDict(frozen=True)

    success: bool = True
    items: list[PaymentStatusResponse]
    pagination: PaginationMeta
    meta: ApiMeta

    @classmethod
    def from_service(
        cls,
        items: list,
        *,
        paginated,
        request_id: str,
    ) -> "PaymentHistoryResponse":
        return cls(
            items=[PaymentStatusResponse.from_orm(tx) for tx in items],
            pagination=PaginationMeta.from_paginated_result(paginated),
            meta=ApiMeta.build(request_id),
        )