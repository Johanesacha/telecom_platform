# app/api/deps.py

"""
FastAPI dependency factories for all route handlers.

This file is the ONLY place in the application where:
  - API key authentication is performed (Bearer token → DB lookup)
  - request.state.api_key is SET (AuditMiddleware reads it post-response)
  - AsyncSession is provided to route handlers
  - Redis client is provided to route handlers (from app.state pool)
  - Pagination parameters are parsed and validated
  - Service instances are constructed with their dependencies

Dependency graph:
  get_db()       ─────────────────────────────────────► AsyncSession
  get_redis()    ─────────────────────────────────────► aioredis.Redis
  get_api_key()  ◄── get_db, get_redis, Request ──────► ApiKey (+ sets request.state)
  get_current_user() ◄── get_api_key, get_db ──────────► User
  require_admin()    ◄── get_current_user ─────────────► User (admin only)
  require_manager_or_admin() ◄── get_current_user ─────► User (manager or admin)
  get_quota_service() ◄── get_api_key, get_redis ───────► QuotaService
  PaginationParams   ◄── Query params ─────────────────► PaginationParams

Security invariants:
  - Bearer token is SHA-256 hashed before DB lookup (raw token never stored)
  - Hash comparison uses hmac.compare_digest (constant-time, no timing oracle)
  - Revoked keys are rejected even if the hash matches
  - 401 vs 403 distinction: 401 = unauthenticated, 403 = unauthorised
  - All auth failures return 401 with a deliberately vague message
    (do not confirm whether the key exists vs is revoked — info leakage)

No business logic lives here — only dependency wiring.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import AsyncGenerator

import redis.asyncio as aioredis
from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.core.exceptions import (
    ResourceNotFoundError,
    RevokedApiKeyError,
)

# HTTPBearer extractor — raises 403 automatically when header is absent
# auto_error=False gives us control over the error response shape
_bearer_scheme = HTTPBearer(auto_error=False)

# Constant-time sentinel for when no token is supplied
# Used in hmac.compare_digest to avoid short-circuit on None
_EMPTY_HASH = "0" * 64  # 64 hex chars = SHA-256 output length


# ── Database session ───────────────────────────────────────────────────────

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Yield a request-scoped AsyncSession.

    Lifecycle:
      - Session created at first use of this dependency
      - Committed on clean exit from the route handler
      - Rolled back if any exception propagates
      - Closed in the finally block regardless

    Usage in route handler:
        @router.post('/sms/send')
        async def send_sms(session: AsyncSession = Depends(get_db)):
            ...

    The session is NOT shared with AuditMiddleware — it creates its own
    independent session. See audit.py for rationale.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        # AsyncSessionLocal context manager handles session.close()


# ── Redis client ───────────────────────────────────────────────────────────

async def get_redis(request: Request) -> AsyncGenerator[aioredis.Redis, None]:
    """
    Yield the shared Redis connection pool from app.state.

    The pool is created in main.py's lifespan startup and stored as
    app.state.redis. This dependency yields the pool reference — no
    new connection is created per request. aioredis pools connections
    internally and returns them on each command.

    Raises RuntimeError if called before lifespan startup (should not
    occur in production; caught during startup tests).

    Usage in route handler:
        @router.get('/quota')
        async def get_quota(redis: aioredis.Redis = Depends(get_redis)):
            ...
    """
    redis_client: aioredis.Redis = request.app.state.redis
    if redis_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis connection pool not initialised",
        )
    yield redis_client
    # Pool lifecycle managed by main.py lifespan — do not close here


# ── API key authentication ─────────────────────────────────────────────────

def _hash_token(raw_token: str) -> str:
    """
    SHA-256 hash a raw API key token.

    The database stores only the hash — the raw token is never persisted.
    This function must produce the same hash as the one stored at key
    creation time in AuthService.create_api_key().

    Returns a 64-character lowercase hex string.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


async def get_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    session: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    """
    Authenticate the request via Bearer API key.

    Flow:
      1. Extract Bearer token from Authorization header
      2. SHA-256 hash the token
      3. Look up ApiKey record by hash via ApiKeyRepository
      4. Constant-time comparison of stored hash vs computed hash
      5. Check key is not revoked
      6. Set request.state.api_key (AuditMiddleware reads this)
      7. Return the ApiKey ORM instance

    Returns:
        ApiKey ORM instance on success.

    Raises:
        HTTP 401: missing token, token not found, revoked key, any error.
                  All failure cases return the same vague message —
                  confirming whether a key exists vs is revoked would be
                  an information leak useful to an attacker enumerating keys.

    Note on rate limiting:
        RateLimitMiddleware already ran at this point. It used the raw
        token's hash to select a Redis bucket without DB access.
        This is the first point where DB lookup confirms the token is valid.
    """
    from app.repositories.api_key_repo import ApiKeyRepository
    # ── Step 1: Require Bearer credentials ────────────────────────────────
    # HTTPBearer with auto_error=False returns None when header is absent
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Provide a Bearer API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    raw_token: str = credentials.credentials

    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Provide a Bearer API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # ── Step 2: Hash the raw token ─────────────────────────────────────────
    computed_hash = _hash_token(raw_token)

    # ── Step 3: DB lookup by hash ──────────────────────────────────────────
    repo = ApiKeyRepository(session)
    try:
        api_key = await repo.get_by_hash(computed_hash)
    except ResourceNotFoundError:
        api_key = None
    except Exception:
        # Unexpected DB error — do not leak details
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # ── Step 4: Constant-time hash comparison ─────────────────────────────
    # Even though we fetched by hash (index lookup), we still compare
    # the stored hash against the computed hash in constant time.
    # This prevents timing oracles in edge cases where the DB returns
    # a row faster for some inputs than others.
    stored_hash: str = api_key.key_hash if hasattr(api_key, "key_hash") else ""
    if not hmac.compare_digest(
        computed_hash.encode("utf-8"),
        stored_hash.encode("utf-8"),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # ── Step 5: Check revocation ───────────────────────────────────────────
    if api_key.is_revoked:
        # Deliberate: same message as invalid key — do not confirm existence
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # ── Step 6: Set request.state — AuditMiddleware reads this ────────────
    # Must happen before this function returns — AuditMiddleware reads it
    # after call_next() completes, which is after this dependency returns.
    request.state.api_key = api_key

    # ── Step 7: Update last_used_at (non-blocking, best-effort) ───────────
    # Fire-and-forget: track last use without blocking the response.
    # If this fails, the key still works — last_used_at is analytics only.
    try:
        await repo.touch_last_used(api_key.id)
    except Exception:
        pass  # Non-critical — never block authentication on analytics

    return api_key


# ── User loading ───────────────────────────────────────────────────────────

async def get_current_user(
    api_key=Depends(get_api_key),
    session: AsyncSession = Depends(get_db),
):
    """
    Load the User record that owns the authenticated API key's application.

    Used by management endpoints that need the user identity, not just
    the application identity. The api_key gives us application_id;
    the application gives us owner_email; the user is found by email
    or by user_id stored on the application.

    Returns:
        User ORM instance.

    Raises:
        HTTP 401: if the owning user cannot be found (account deleted).
    """
    from app.repositories.application_repo import ApplicationRepository
    from app.repositories.user_repo import UserRepository

    app_repo = ApplicationRepository(session)
    user_repo = UserRepository(session)

    try:
        application = await app_repo.get_by_id(str(api_key.application_id))
    except (ResourceNotFoundError, Exception):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key application not found.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user = await user_repo.get_by_id(str(application.user_id))
    except (ResourceNotFoundError, Exception):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key owner account not found.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


# ── Role enforcement ───────────────────────────────────────────────────────

async def require_admin(current_user=Depends(get_current_user)):
    """
    Require the authenticated user to have ADMIN role.

    Returns:
        User ORM instance (passthrough — callers receive the user directly).

    Raises:
        HTTP 403: if user.role is not ADMIN.
    """
    role = current_user.role
    role_str = role.value if hasattr(role, "value") else str(role)

    if role_str.upper() != "ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "This endpoint requires ADMIN role. "
                f"Your role: {role_str}."
            ),
        )
    return current_user


async def require_manager_or_admin(current_user=Depends(get_current_user)):
    """
    Require the authenticated user to have MANAGER or ADMIN role.

    Returns:
        User ORM instance.

    Raises:
        HTTP 403: if user.role is neither MANAGER nor ADMIN.
    """
    role = current_user.role
    role_str = role.value if hasattr(role, "value") else str(role)

    if role_str.upper() not in {"MANAGER", "ADMIN"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "This endpoint requires MANAGER or ADMIN role. "
                f"Your role: {role_str}."
            ),
        )
    return current_user


# ── Pagination ─────────────────────────────────────────────────────────────

@dataclass
class PaginationParams:
    """
    Parsed and validated pagination query parameters.

    FastAPI resolves this as a dependency via Depends():
        pagination: PaginationParams = Depends()

    Query parameters accepted:
        page:      Current page number (1-based, default 1, min 1)
        page_size: Items per page (default 20, min 1, max 100)

    Computed fields:
        skip: records to skip = (page - 1) * page_size
              Used directly as the offset in repository list() calls.

    Example:
        page=3, page_size=20 → skip=40 (records 41–60)

    Usage in route handler:
        @router.get('/sms/history')
        async def list_history(pagination: PaginationParams = Depends()):
            items, total = await sms_svc.list_history(
                skip=pagination.skip,
                limit=pagination.page_size,
            )
    """

    page: int = Query(default=1, ge=1, description="Page number (1-based)")
    page_size: int = Query(
        default=20,
        ge=1,
        le=100,
        description="Items per page (max 100)",
    )

    @property
    def skip(self) -> int:
        """Offset for DB queries: (page - 1) * page_size."""
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        """Alias for page_size — matches SQLAlchemy .limit() parameter name."""
        return self.page_size


# ── Service factories ──────────────────────────────────────────────────────

async def get_quota_service(
    api_key=Depends(get_api_key),
    redis: aioredis.Redis = Depends(get_redis),
):
    """
    Construct a QuotaService for the authenticated API key.

    QuotaService constructor: QuotaService(api_key, redis)
    BUG NOTE: QuotaService takes api_key as FIRST positional argument.
    Do NOT call QuotaService(redis) or QuotaService(redis=redis).

    Returns:
        QuotaService instance scoped to the authenticated application.
    """
    from app.services.quota_service import QuotaService

    return QuotaService(api_key, redis)


async def get_sms_service(
    session: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    api_key=Depends(get_api_key),
):
    """
    Construct SMSService for the authenticated API key.

    Returns:
        SMSService instance.
    """
    from app.services.sms_service import SMSService
    from app.services.quota_service import QuotaService

    quota_svc = QuotaService(api_key, redis)
    return SMSService(session=session, quota_service=quota_svc, api_key=api_key)


async def get_ussd_service(
    session: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    api_key=Depends(get_api_key),
):
    """Construct USSDService for the authenticated API key."""
    from app.services.ussd_service import USSDService
    from app.services.quota_service import QuotaService

    quota_svc = QuotaService(api_key, redis)
    return USSDService(session=session, quota_service=quota_svc, api_key=api_key)


async def get_payment_service(
    session: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    api_key=Depends(get_api_key),
):
    """Construct PaymentService for the authenticated API key."""
    from app.services.payment_service import PaymentService
    from app.services.quota_service import QuotaService

    quota_svc = QuotaService(api_key, redis)
    return PaymentService(session=session, quota_service=quota_svc, api_key=api_key)


async def get_number_service(
    session: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    api_key=Depends(get_api_key),
):
    """Construct NumberService for the authenticated API key."""
    from app.services.number_service import NumberService
    from app.services.quota_service import QuotaService

    quota_svc = QuotaService(api_key, redis)
    return NumberService(session=session, quota_service=quota_svc, api_key=api_key, redis=redis)


async def get_notification_service(
    session: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    api_key=Depends(get_api_key),
):
    """Construct NotificationService for the authenticated API key."""
    from app.services.notification_service import NotificationService
    from app.services.quota_service import QuotaService

    quota_svc = QuotaService(api_key, redis)
    return NotificationService(session=session, quota_service=quota_svc, api_key=api_key)


async def get_auth_service(
    session: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    """
    Construct AuthService — no API key required.

    Auth endpoints (register, login, token refresh) are pre-authentication
    by definition — they cannot require get_api_key().
    """
    from app.services.auth_service import AuthService

    return AuthService(session=session, redis=redis)


async def get_audit_service(
    session: AsyncSession = Depends(get_db),
):
    """Construct AuditService for monitoring route handlers."""
    from app.services.audit_service import AuditService

    return AuditService(session=session)