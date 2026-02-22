"""
API key request and response schemas.

Security architecture — two response models are non-negotiable:

  CreateApiKeyResponse: returned by POST /keys and POST /keys/{id}/rotate ONLY.
    Contains raw_key — the full secret, shown once, never stored.

  ApiKeyResponse: returned by GET /keys, GET /keys/{id}, and all other reads.
    Does NOT contain raw_key — the field does not exist on this model.
    A bug that accidentally tries to include the raw key in a list response
    will produce a validation error, not a silent key leak.

The type system enforces the security rule. It cannot be bypassed accidentally.

key_prefix:
  The first 12 characters of the raw key, stored permanently and returned
  in all responses. Allows the developer to identify which key they are
  looking at without exposing the secret. Acts as a display identifier.
  Example: "tp_live_a1b2c3" (prefix) vs "tp_live_a1b2c3...64more chars" (raw).
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.security.scopes import Scope


# Maximum number of scopes allowed per key — defence against scope bloat
_MAX_SCOPES_PER_KEY: int = 10


# ── Request Schemas ────────────────────────────────────────────────────────

class CreateKeyRequest(BaseModel):
    """
    Request body for POST /keys (create additional API key).

    Scopes must be a subset of the plan's entitlement.
    The service layer validates scope-plan compatibility and raises
    InsufficientScopeError if any requested scope exceeds the plan.
    This schema validates format and presence only.
    """
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(
        min_length=2,
        max_length=100,
        description="Descriptive name for this key (e.g. 'Production Server')",
    )
    key_type: str = Field(
        default="LIVE",
        description="Key type: LIVE or SANDBOX",
    )
    scopes: list[str] = Field(
        description=(
            "List of permission scopes. Must be within your plan's entitlement. "
            f"Valid scopes: {[s.value for s in Scope]}"
        )
    )

    @field_validator("key_type")
    @classmethod
    def validate_key_type(cls, v: str) -> str:
        allowed = {"LIVE", "SANDBOX"}
        normalised = v.upper().strip()
        if normalised not in allowed:
            raise ValueError(f"key_type must be LIVE or SANDBOX. Got: '{v}'")
        return normalised

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("At least one scope is required")
        if len(v) > _MAX_SCOPES_PER_KEY:
            raise ValueError(
                f"Maximum {_MAX_SCOPES_PER_KEY} scopes per key. Got {len(v)}."
            )
        valid_scopes = {s.value for s in Scope}
        invalid = [s for s in v if s not in valid_scopes]
        if invalid:
            raise ValueError(
                f"Unknown scope(s): {invalid}. "
                f"Valid scopes: {sorted(valid_scopes)}"
            )
        # Deduplicate preserving order
        seen: set[str] = set()
        deduped = []
        for s in v:
            if s not in seen:
                seen.add(s)
                deduped.append(s)
        return deduped


# ── Response Schemas ───────────────────────────────────────────────────────

class ApiKeyResponse(BaseModel):
    """
    Serialised API key for read operations.

    raw_key is NOT present on this model. It cannot appear in any response
    that uses ApiKeyResponse — including list responses, status responses,
    and rotation responses for the OLD key.

    This is used by:
      GET  /keys          → list all keys (KeyListResponse wraps this)
      GET  /keys/{id}     → single key detail
      All management reads

    key_prefix: allows the developer to identify which key this is
    without revealing the secret. Display it as "tp_live_a1b2c3..." in UIs.
    """
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    key_prefix: str = Field(
        description="First 12 characters of the key — safe to display, not the secret"
    )
    key_type: str = Field(description="LIVE or SANDBOX")
    scopes: list[str] = Field(description="Permission scopes granted to this key")
    is_revoked: bool
    application_id: str
    created_at: str
    last_used_at: str | None = None
    expires_at: str | None = None

    @classmethod
    def from_orm(cls, key) -> "ApiKeyResponse":
        """
        Construct from a SQLAlchemy ApiKey ORM instance.

        Handles UUID → str and datetime → ISO str explicitly.
        key_type and scopes may be enums or strings — handled both ways.
        """
        return cls(
            id=str(key.id),
            name=key.name if key.name else "",
            key_prefix=key.key_prefix,
            key_type=key.key_type.value if hasattr(key.key_type, "value") else str(key.key_type),
            scopes=list(key.scopes) if key.scopes else [],
            is_revoked=key.is_revoked,
            application_id=str(key.application_id),
            created_at=key.created_at.isoformat() if key.created_at else "",
            last_used_at=key.last_used_at.isoformat() if key.last_used_at else None,
            expires_at=key.expires_at.isoformat() if key.expires_at else None,
        )


class CreateApiKeyResponse(BaseModel):
    """
    Response for key CREATION operations only.

    raw_key IS present on this model. It contains the complete API key
    string that the developer must store immediately.

    This model is used by:
      POST /keys           → create new key
      POST /keys/{id}/rotate → create replacement key

    After these responses are delivered, the raw key is gone permanently.
    The developer must store it in their secrets manager / .env file now.

    The raw_key field is annotated with explicit instructions.
    The message field reinforces the one-time nature.
    """
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    key_prefix: str = Field(
        description="First 12 characters of the key (permanent, safe to display)"
    )
    raw_key: str = Field(
        description=(
            "Complete API key — shown ONCE only. "
            "Store it in a secrets manager immediately. "
            "This value cannot be retrieved after this response."
        )
    )
    key_type: str
    scopes: list[str]
    application_id: str
    created_at: str
    message: str = Field(
        default=(
            "Your API key has been created and is shown below exactly once. "
            "Store it securely — it cannot be retrieved after this response. "
            "If lost, you must rotate this key."
        )
    )

    @classmethod
    def from_service(
        cls,
        key,
        raw_key: str,
    ) -> "CreateApiKeyResponse":
        """
        Build from AuthService.create_api_key() return values.

        Takes the ORM ApiKey instance and the raw key string returned
        by the service. Both are required — neither can be None.
        """
        return cls(
            id=str(key.id),
            name=key.name if key.name else "",
            key_prefix=key.key_prefix,
            raw_key=raw_key,
            key_type=key.key_type.value if hasattr(key.key_type, "value") else str(key.key_type),
            scopes=list(key.scopes) if key.scopes else [],
            application_id=str(key.application_id),
            created_at=key.created_at.isoformat() if key.created_at else "",
        )


class KeyListResponse(BaseModel):
    """
    Paginated list of API keys for an application.

    Returns ALL keys — active and revoked — so developers can see
    their rotation history and identify which keys to clean up.

    active_count and revoked_count are pre-computed to avoid requiring
    the client to iterate the list to compute these common UI metrics.
    """
    model_config = ConfigDict(frozen=True)

    items: list[ApiKeyResponse]
    total: int = Field(description="Total number of keys (active + revoked)")
    active_count: int = Field(description="Keys that are not revoked")
    revoked_count: int = Field(description="Keys that have been revoked")

    @classmethod
    def from_keys(cls, keys: list) -> "KeyListResponse":
        """Build from a list of SQLAlchemy ApiKey ORM instances."""
        items = [ApiKeyResponse.from_orm(k) for k in keys]
        active = sum(1 for k in keys if not k.is_revoked)
        return cls(
            items=items,
            total=len(items),
            active_count=active,
            revoked_count=len(items) - active,
        )