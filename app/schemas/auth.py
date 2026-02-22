"""
Authentication and user management schemas.

LoginRequest note:
  The token endpoint (POST /auth/token) does NOT use LoginRequest as its
  request body. It uses FastAPI's OAuth2PasswordRequestForm (form data).
  LoginRequest exists here as a documentation schema only — it is referenced
  in json_schema_extra for the Swagger UI description of the endpoint.

  This distinction is critical:
    - OAuth2PasswordRequestForm: Content-Type: application/x-www-form-urlencoded
    - A Pydantic BaseModel body: Content-Type: application/json
  Standard OAuth2 clients (Postman, swagger-ui Authorize button) send form data.
  Using a JSON body would break all standard OAuth2 client integrations.

UserResponse:
  hashed_password is intentionally absent — omission is the whitelist mechanism.
  Pydantic model_config from_attributes=True allows direct construction from
  SQLAlchemy User ORM instances. Only declared fields are serialised.

RegisterApplicationResponse:
  raw_live_key and raw_sandbox_key are present here (application registration)
  because this is the only moment both keys are available. After this response,
  neither key is retrievable. The response must make this explicit.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


# ── Request Schemas ────────────────────────────────────────────────────────

class UserRegisterRequest(BaseModel):
    """
    Request body for POST /auth/users (admin-only: create operator account).

    Not the same as developer application registration.
    This creates a User with MANAGER or ADMIN role for the management console.
    """
    model_config = ConfigDict(str_strip_whitespace=True)

    email: EmailStr = Field(
        description="Email address for the new operator account"
    )
    password: str = Field(
        min_length=12,
        description="Password (minimum 12 characters)",
    )
    full_name: str = Field(
        min_length=2,
        max_length=100,
        description="Full name of the operator",
    )
    role: str = Field(
        default="MANAGER",
        description="Role to assign: MANAGER or ADMIN",
    )

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        allowed = {"MANAGER", "ADMIN"}
        normalised = v.upper().strip()
        if normalised not in allowed:
            raise ValueError(
                f"role must be one of: {sorted(allowed)}. Got: '{v}'"
            )
        return normalised

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        """
        Basic strength check. Full entropy analysis is out of scope.
        Enforces: minimum length (covered by Field), at least one digit,
        at least one letter. No maximum — bcrypt handles any length.
        """
        has_letter = any(c.isalpha() for c in v)
        has_digit = any(c.isdigit() for c in v)
        if not has_letter or not has_digit:
            raise ValueError(
                "Password must contain at least one letter and one digit"
            )
        return v


class RegisterApplicationRequest(BaseModel):
    """
    Request body for POST /auth/register (developer self-service).

    Creates a ClientApplication (FREE plan) with one LIVE + one SANDBOX key.
    The raw key strings are returned exactly once in RegisterApplicationResponse.
    """
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(
        min_length=2,
        max_length=100,
        description="Application name (e.g. 'My Payment App')",
    )
    owner_email: EmailStr = Field(
        description="Developer email address — used for account recovery and billing"
    )
    description: str | None = Field(
        default=None,
        max_length=500,
        description="Optional description of the application",
    )


class LoginRequest(BaseModel):
    """
    Documentation schema for the token endpoint.

    NOT used as the actual request body — the route uses OAuth2PasswordRequestForm.
    This schema exists to populate json_schema_extra for Swagger UI display.

    The actual POST /auth/token request must be:
      Content-Type: application/x-www-form-urlencoded
      Body: username=<email>&password=<password>
    """
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "username": "admin@esmt.sn",
                "password": "SecurePass123",
            }
        }
    )

    username: str = Field(description="Email address (sent as 'username' per OAuth2 spec)")
    password: str = Field(description="Account password")


class RefreshRequest(BaseModel):
    """Request body for POST /auth/refresh."""
    model_config = ConfigDict(str_strip_whitespace=True)

    refresh_token: str = Field(
        description="Valid refresh token received from login or previous refresh"
    )


class ChangeRoleRequest(BaseModel):
    """Request body for PATCH /auth/users/{user_id}/role (admin-only)."""
    model_config = ConfigDict(str_strip_whitespace=True)

    role: str = Field(description="New role: MANAGER or ADMIN")

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        allowed = {"MANAGER", "ADMIN"}
        normalised = v.upper().strip()
        if normalised not in allowed:
            raise ValueError(f"role must be one of: {sorted(allowed)}")
        return normalised


# ── Response Schemas ───────────────────────────────────────────────────────

class UserResponse(BaseModel):
    """
    Serialised User for management responses.

    hashed_password is intentionally absent — field omission is the
    security mechanism. model_validate(orm_user) will serialise only
    the fields declared here, regardless of what the ORM object contains.

    id and created_at are serialised as strings:
      - UUID → str: avoids JSON serialisation ambiguity across platforms
      - datetime → str: ISO 8601 with UTC offset, never naive
    """
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    full_name: str
    role: str
    is_active: bool
    created_at: str

    @classmethod
    def from_orm(cls, user) -> "UserResponse":
        """
        Construct from a SQLAlchemy User ORM instance.

        Handles UUID → str and datetime → ISO str conversions explicitly
        rather than relying on json_encoders (deprecated in Pydantic v2).
        """
        return cls(
            id=str(user.id),
            email=user.email,
            full_name=user.full_name,
            role=user.role.value if hasattr(user.role, "value") else str(user.role),
            is_active=user.is_active,
            created_at=user.created_at.isoformat() if user.created_at else "",
        )


class TokenResponse(BaseModel):
    """
    JWT token pair returned by login and token refresh.

    Contains:
      access_token:  Short-lived JWT (15 min) for authenticated requests.
                     Send as: Authorization: Bearer <access_token>
      refresh_token: Long-lived JWT (7 days) for obtaining new access tokens.
                     Send to POST /auth/refresh when access_token expires.
                     Store securely — a stolen refresh token enables indefinite
                     session access until the next rotation.
      token_type:    Always "bearer" per OAuth2 spec.
      expires_in:    Access token lifetime in seconds (900 = 15 minutes).
      user_id:       UUID of the authenticated user as string.
      role:          Role of the authenticated user (MANAGER or ADMIN).
    """
    model_config = ConfigDict(frozen=True)

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access token lifetime in seconds")
    user_id: str = Field(description="Authenticated user UUID as string")
    role: str = Field(description="User role: MANAGER or ADMIN")


class RegisterApplicationResponse(BaseModel):
    """
    Response to successful developer application registration.

    raw_live_key and raw_sandbox_key are present here and ONLY here.
    These strings are shown to the developer exactly once.
    After this response is delivered, they cannot be retrieved.

    The response must make this explicit — the message field confirms it.
    """
    model_config = ConfigDict(frozen=True)

    application_id: str = Field(description="UUID of the created application")
    name: str
    owner_email: str
    plan: str = "FREE"

    raw_live_key: str = Field(
        description=(
            "LIVE API key — shown exactly once. "
            "Store it securely now — it cannot be retrieved after this response."
        )
    )
    raw_sandbox_key: str = Field(
        description=(
            "SANDBOX API key — shown exactly once. "
            "Store it securely now — it cannot be retrieved after this response."
        )
    )
    message: str = Field(
        default=(
            "Your API keys have been created. "
            "Both keys are shown exactly once — store them securely now. "
            "If lost, you must rotate your keys."
        )
    )

    @classmethod
    def from_service(
        cls,
        application,
        raw_live_key: str,
        raw_sandbox_key: str,
    ) -> "RegisterApplicationResponse":
        """Build from AuthService.register_application() return values."""
        return cls(
            application_id=str(application.id),
            name=application.name,
            owner_email=application.owner_email,
            plan=application.plan.value if hasattr(application.plan, "value") else str(application.plan),
            raw_live_key=raw_live_key,
            raw_sandbox_key=raw_sandbox_key,
        )