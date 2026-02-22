"""
FastAPI dependency factories for all route handlers.
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

_bearer_scheme = HTTPBearer(auto_error=False)
_EMPTY_HASH = "0" * 64


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_redis(request: Request) -> AsyncGenerator[aioredis.Redis, None]:
    redis_client: aioredis.Redis = request.app.state.redis
    if redis_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis connection pool not initialised",
        )
    yield redis_client


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


async def get_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    session: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    from app.repositories.api_key_repo import ApiKeyRepository

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

    computed_hash = _hash_token(raw_token)

    repo = ApiKeyRepository(session)
    try:
        key_prefix = raw_token[:12]
        api_key = await repo.get_active_by_prefix(key_prefix)
    except ResourceNotFoundError:
        api_key = None
    except Exception:
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

    if api_key.is_revoked:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    request.state.api_key = api_key

    try:
        await repo.touch_last_used(api_key.id)
    except Exception:
        pass

    return api_key


async def get_current_user(
    api_key=Depends(get_api_key),
    session: AsyncSession = Depends(get_db),
):
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


async def require_admin(current_user=Depends(get_current_user)):
    role = current_user.role
    role_str = role.value if hasattr(role, "value") else str(role)
    if role_str.upper() != "ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires ADMIN role. Your role: " + role_str,
        )
    return current_user


async def require_manager_or_admin(current_user=Depends(get_current_user)):
    role = current_user.role
    role_str = role.value if hasattr(role, "value") else str(role)
    if role_str.upper() not in {"MANAGER", "ADMIN"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires MANAGER or ADMIN role. Your role: " + role_str,
        )
    return current_user


@dataclass
class PaginationParams:
    page: int = Query(default=1, ge=1, description="Page number (1-based)")
    page_size: int = Query(default=20, ge=1, le=100, description="Items per page (max 100)")

    @property
    def skip(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


async def get_quota_service(
    api_key=Depends(get_api_key),
    redis: aioredis.Redis = Depends(get_redis),
):
    from app.services.quota_service import QuotaService
    return QuotaService(api_key, redis)


async def get_sms_service(
    session: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    api_key=Depends(get_api_key),
):
    from app.services.sms_service import SMSService
    return SMSService(session=session, redis=redis, api_key=api_key)


async def get_ussd_service(
    session: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    api_key=Depends(get_api_key),
):
    from app.services.ussd_service import USSDService
    return USSDService(session=session, redis=redis, api_key=api_key)


async def get_payment_service(
    session: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    api_key=Depends(get_api_key),
):
    from app.services.payment_service import PaymentService
    return PaymentService(session=session, redis=redis, api_key=api_key)


async def get_number_service(
    session: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    api_key=Depends(get_api_key),
):
    from app.services.number_service import NumberService
    return NumberService(session=session, redis=redis, api_key=api_key)


async def get_notification_service(
    session: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    api_key=Depends(get_api_key),
):
    from app.services.notification_service import NotificationService
    return NotificationService(session=session, redis=redis, api_key=api_key)


async def get_auth_service(
    session: AsyncSession = Depends(get_db),
):
    from app.services.auth_service import AuthService
    return AuthService(session=session)


async def get_audit_service(
    session: AsyncSession = Depends(get_db),
):
    from app.services.audit_service import AuditService
    return AuditService(session=session)