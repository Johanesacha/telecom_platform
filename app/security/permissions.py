"""
FastAPI dependency factories for authentication and authorisation.

Two authentication paths coexist:
  API Key path  → get_api_key() → require_scope()  — for telecom service routes
  JWT path      → get_current_user() → require_role() — for management routes

Dependency resolution order (FastAPI handles this automatically):
  require_scope(scope) → get_api_key() → get_db(), get_redis()
  require_role(*roles) → get_current_user() → get_db()

Security bugs in this file are silent and exploitable.
"""
from __future__ import annotations

import hashlib
from typing import Callable
from uuid import UUID

from fastapi import Depends, Header, Request
from fastapi.security import OAuth2PasswordBearer

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import (
    AuthenticationError,
    ExpiredApiKeyError,
    InsufficientRoleError,
    InsufficientScopeError,
    InvalidApiKeyError,
    RevokedApiKeyError,
)
from app.domain.api_key import ApiKey
from app.domain.user import User, UserRole
from app.repositories.api_key_repo import ApiKeyRepository
from app.repositories.user_repo import UserRepository
from app.security.api_key import extract_prefix, verify_api_key
from app.security.jwt import verify_token
from app.security.scopes import Scope
from app.utils.time_utils import utcnow


# OAuth2 scheme — extracts Bearer token from Authorization header
# auto_error=False means we raise our own typed exception instead of
# FastAPI's generic 401. This keeps all errors in our standard envelope.
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/api/v1/auth/token",
    auto_error=False,
)


# ── API Key Authentication ─────────────────────────────────────────────────

async def get_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
) -> ApiKey:
    """
    Authenticate an incoming request via its X-API-Key header.

    Steps:
      1. Confirm header is present — 401 if missing
      2. Extract prefix (first 12 chars) for indexed DB lookup
      3. Fetch non-revoked key for active application — 401 if not found
      4. Timing-safe hash comparison — 401 if mismatch
      5. Check expiration — 401 if expired
      6. Store key on request.state for middleware access
      7. Return authenticated ApiKey instance

    The same InvalidApiKeyError is raised for "prefix not found" and
    "hash mismatch" cases. This prevents timing-based key enumeration:
    an attacker cannot distinguish between an unknown prefix and a known
    prefix with the wrong secret by observing response timing or content.
    """
    if not x_api_key:
        raise AuthenticationError("X-API-Key header is required")

    prefix = extract_prefix(x_api_key)
    if not prefix:
        raise InvalidApiKeyError("Malformed API key")

    repo = ApiKeyRepository(db)
    api_key = await repo.get_active_by_prefix(prefix)

    # Always compute the hash even when api_key is None.
    # This ensures the response time is identical whether the prefix
    # exists or not — preventing timing oracle attacks.
    provided_hash = hashlib.sha256(x_api_key.encode("utf-8")).hexdigest()
    stored_hash = api_key.key_hash if api_key is not None else "0" * 64

    if not verify_api_key(x_api_key, stored_hash) or api_key is None:
        raise InvalidApiKeyError()

    # Expiration check — performed after hash verification so we do not
    # reveal whether a key exists via the error code difference.
    if api_key.expires_at is not None and api_key.expires_at < utcnow():
        raise ExpiredApiKeyError()

    # Store on request.state for AuditMiddleware and RateLimitMiddleware.
    # These middlewares read request.state.api_key after auth resolves.
    request.state.api_key = api_key

    return api_key


def require_scope(scope: Scope) -> Callable:
    """
    Factory that returns a FastAPI dependency enforcing a specific scope.

    Usage at route definition:
        @router.post("/sms/send")
        async def send_sms(
            api_key: ApiKey = Depends(require_scope(Scope.SMS_SEND)),
        ): ...

    The returned dependency:
      1. Authenticates the request via get_api_key()
      2. Checks that the required scope is present on the key
      3. Returns the authenticated ApiKey on success
      4. Raises InsufficientScopeError (HTTP 403, AUTH_005) if absent

    Fail-closed: any exception in get_api_key() propagates up before
    the scope check is reached. A key that fails authentication never
    reaches the scope check — no partial access is possible.
    """
    async def dependency(
        api_key: ApiKey = Depends(get_api_key),
    ) -> ApiKey:
        if scope not in (api_key.scopes or []):
            raise InsufficientScopeError(
                f"This operation requires the '{scope}' scope. "
                f"Current key scopes: {api_key.scopes}"
            )
        return api_key

    # Rename the inner function to reflect the scope it enforces.
    # FastAPI uses the function name in OpenAPI dependency display.
    dependency.__name__ = f"require_scope_{scope.replace(':', '_')}"
    return dependency


# ── JWT Authentication ─────────────────────────────────────────────────────

async def get_current_user(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Authenticate a management request via its JWT Bearer token.

    Steps:
      1. Confirm token is present — 401 if missing
      2. verify_token() — validates RS256 signature, exp, iat, type claim
      3. Extract user UUID from 'sub' claim
      4. Fetch User from database — 401 if not found
      5. Confirm user is active — 401 if deactivated
      6. Return authenticated User instance

    Token expiry (15 minutes) is enforced by verify_token() which
    checks the 'exp' claim against the current UTC time.

    Role claims in the JWT are NOT trusted for authorisation —
    the role is always re-fetched from the database. A role change
    takes effect on the next request even within a token's lifetime
    (unlike if we read role from the JWT payload).
    """
    if not token:
        raise AuthenticationError(
            "Authorization header with Bearer token is required"
        )

    payload = verify_token(token, expected_type="access")

    try:
        user_id = UUID(payload["sub"])
    except (KeyError, ValueError):
        raise AuthenticationError("Token 'sub' claim is missing or invalid")

    repo = UserRepository(db)
    user = await repo.get_by_id(user_id)

    if user is None:
        raise AuthenticationError("User account not found")

    if not user.is_active:
        raise AuthenticationError("User account is deactivated")

    return user


def require_role(*roles: UserRole) -> Callable:
    """
    Factory that returns a FastAPI dependency enforcing one of the given roles.

    Usage at route definition:
        @router.get("/monitoring/stats")
        async def get_stats(
            user: User = Depends(require_role(UserRole.MANAGER, UserRole.ADMIN)),
        ): ...

    Multiple roles are accepted as positional arguments. The authenticated
    user must have ANY ONE of the listed roles (OR logic, not AND).

    The returned dependency:
      1. Authenticates the request via get_current_user()
      2. Checks that user.role is in the set of allowed roles
      3. Returns the authenticated User on success
      4. Raises InsufficientRoleError (HTTP 403, AUTH_006) if not allowed

    Fail-closed: authentication failure in get_current_user() propagates
    before the role check is reached.
    """
    allowed = frozenset(roles)

    async def dependency(
        user: User = Depends(get_current_user),
    ) -> User:
        if user.role not in allowed:
            raise InsufficientRoleError(
                f"This operation requires one of: "
                f"{[r.value for r in allowed]}. "
                f"Your role: {user.role}"
            )
        return user

    role_names = "_or_".join(r.value for r in roles)
    dependency.__name__ = f"require_role_{role_names}"
    return dependency