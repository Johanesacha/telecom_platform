"""
Number verification request and response schemas.

from_cache semantics:
  True  → result served from Redis (Tier 1) or DB rehydration (Tier 2).
          No quota was consumed for this request.
  False → full verification performed (Tier 3).
          One quota unit was consumed and a new DB record was created.
  Clients should surface this to help developers understand their quota usage.

is_valid vs is_active distinction:
  is_valid: phonenumbers library confirmed the number is a syntactically
            valid E.164 number for the given country. Always True in
            successful responses (invalid numbers raise 422 before this schema).
  is_active: simulated liveness check. True means the number is likely
             active on the network (in production, this would be an HLR lookup).
             In sandbox mode: even last digit → True, odd → False.

country_hint:
  ISO 3166-1 alpha-2 code (2 uppercase letters) passed to phonenumbers.parse()
  as the default country context for local-format numbers.
  'SN' (Senegal) is the default — correct for the primary market.
  Developers processing international numbers should override this.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import ApiMeta, PaginationMeta


# ── Request Schemas ────────────────────────────────────────────────────────

class NumberVerifyRequest(BaseModel):
    """
    Request body for POST /numbers/verify.

    msisdn: phone number in any reasonable format.
            The service normalises to E.164 using country_hint as context.
            Validated here as non-empty string only — full phonenumbers
            validation happens in NumberService.verify() via parse_msisdn().
            A failed parse raises InvalidMSISDNError → 422 with clear message.

    country_hint: ISO 3166-1 alpha-2 code to resolve local-format numbers.
                  'SN' resolves '77 123 45 67' as a Senegalese number.
                  'FR' would resolve it as a French number (invalid — wrong length).
                  Exactly 2 characters, normalised to uppercase.
    """
    model_config = ConfigDict(str_strip_whitespace=True)

    msisdn: str = Field(
        description=(
            "Phone number to verify. Accepts any reasonable format: "
            "'77 123 45 67', '+221771234567', '00221771234567'. "
            "Normalised to E.164 before lookup and storage."
        )
    )
    country_hint: str = Field(
        default="SN",
        min_length=2,
        max_length=2,
        description=(
            "ISO 3166-1 alpha-2 country code for resolving local-format numbers. "
            "Default 'SN' (Senegal). "
            "Use 'FR' for French numbers, 'CI' for Côte d'Ivoire, etc."
        ),
    )

    @field_validator("msisdn")
    @classmethod
    def validate_msisdn_non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("msisdn cannot be empty")
        if len(v) > 30:
            raise ValueError(
                f"msisdn is too long ({len(v)} characters). "
                "E.164 numbers have at most 15 digits plus '+' prefix."
            )
        return v

    @field_validator("country_hint")
    @classmethod
    def normalise_country_hint(cls, v: str) -> str:
        """
        Normalise country hint to uppercase.

        'sn' → 'SN', 'fr' → 'FR', 'ci' → 'CI'.
        Exactly 2 characters enforced by Field(min_length=2, max_length=2).
        This validator only handles case normalisation.
        """
        normalised = v.upper().strip()
        if not normalised.isalpha():
            raise ValueError(
                f"country_hint must contain only letters. Got: '{v}'"
            )
        return normalised


# ── Response Schemas ───────────────────────────────────────────────────────

class NumberVerifyResponse(BaseModel):
    """
    Verification result for a single MSISDN.

    All fields are populated regardless of which cache tier answered:
      Tier 1 (Redis):     from_cache=True,  no quota consumed
      Tier 2 (DB rehyd):  from_cache=False, no quota consumed
      Tier 3 (full):      from_cache=False, one quota unit consumed

    operator: one of ORANGE, FREE, EXPRESSO, UNKNOWN.
              UNKNOWN means the prefix was not in the operator table —
              either an international number or an unrecognised Senegalese prefix.

    line_type: one of MOBILE, FIXED, VOIP, UNKNOWN.
               Mobile lines support SMS and mobile money.
               Fixed and VOIP lines cannot receive SMS in most configurations.

    national_format: the number formatted for display in the source country.
                     '77 123 45 67' for Senegalese numbers.
                     Suitable for user-facing display — not for API calls.
    """
    model_config = ConfigDict(frozen=True)

    raw_input: str | None = Field(
        default=None,
        description="The original input string as received"
    )
    msisdn_e164: str = Field(
        description="Normalised E.164 format (e.g. '+221771234567')"
    )
    is_valid: bool = Field(
        description="True if the number is a syntactically valid E.164 number"
    )
    is_active: bool = Field(
        description=(
            "True if the number is likely active on the network. "
            "In production: HLR lookup result. "
            "In this version: True for mobile numbers (VOIP/fixed: False)."
        )
    )
    operator: str = Field(
        description="Detected mobile operator: ORANGE, FREE, EXPRESSO, or UNKNOWN"
    )
    line_type: str = Field(
        description="Line type: MOBILE, FIXED, VOIP, or UNKNOWN"
    )
    country_code: str = Field(
        description="International dialling code (e.g. '+221')"
    )
    national_format: str = Field(
        description="Number in national format for display (e.g. '77 123 45 67')"
    )
    country_iso: str = Field(
        description="ISO 3166-1 alpha-2 country code (e.g. 'SN')"
    )
    from_cache: bool = Field(
        description=(
            "True if this result was served from cache (Redis or recent DB record). "
            "False if a full verification was performed (quota was consumed)."
        )
    )
    is_sandbox: bool = Field(
        description="True if this verification was performed with a sandbox API key"
    )

    @classmethod
    def from_service(cls, result: dict) -> "NumberVerifyResponse":
        """
        Build from the dict returned by NumberService.verify().

        NumberService.verify() returns a dict regardless of which tier
        answered — all tiers produce the same dict shape.
        See NumberService._build_live_result() and _build_sandbox_result().
        """
        return cls(
            raw_input=result.get("raw_input"),
            msisdn_e164=result["msisdn_e164"],
            is_valid=result["is_valid"],
            is_active=result["is_active"],
            operator=str(result["operator"].value)
                if hasattr(result["operator"], "value")
                else str(result["operator"]),
            line_type=str(result["line_type"].value)
                if hasattr(result["line_type"], "value")
                else str(result["line_type"]),
            country_code=result["country_code"],
            national_format=result["national_format"],
            country_iso=result["country_iso"],
            from_cache=result.get("from_cache", False),
            is_sandbox=result["is_sandbox"],
        )


class NumberVerifyHistoryResponse(BaseModel):
    """
    Paginated verification history for GET /numbers/history.
    """
    model_config = ConfigDict(frozen=True)

    success: bool = True
    items: list[NumberVerifyResponse]
    pagination: PaginationMeta
    meta: ApiMeta

    @classmethod
    def from_service(
        cls,
        items: list,
        *,
        paginated,
        request_id: str,
    ) -> "NumberVerifyHistoryResponse":
        """Build from list of NumberVerification ORM instances."""
        schema_items = []
        for record in items:
            result_dict = {
                "raw_input": record.raw_input,
                "msisdn_e164": record.msisdn_e164 or "",
                "is_valid": record.is_valid,
                "is_active": record.is_active,
                "operator": record.operator,
                "line_type": record.line_type,
                "country_code": record.country_code or "",
                "national_format": record.national_format or "",
                "country_iso": record.country_hint or "SN",
                "from_cache": False,
                "is_sandbox": record.is_sandbox,
            }
            schema_items.append(NumberVerifyResponse.from_service(result_dict))
        return cls(
            items=schema_items,
            pagination=PaginationMeta.from_paginated_result(paginated),
            meta=ApiMeta.build(request_id),
        )