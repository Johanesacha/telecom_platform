"""
API key management endpoints.

SIGNATURE CORRECTIONS vs Claude's original:
  AuthService methods take application= (ClientApplication ORM object),
  not application_id= (string). Routes must load the application first.

  list_api_keys(application=app)
  create_api_key(application=app, key_type=, scopes=, name=)
  revoke_api_key(key_id=UUID(id), application=app)
  rotate_api_key(key_id=UUID(id), application=app)
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_api_key, get_auth_service, get_db
from app.schemas.api_key import (
    ApiKeyResponse,
    CreateApiKeyResponse,
    CreateKeyRequest,
    KeyListResponse,
)
from app.schemas.common import ApiResponse

router = APIRouter(prefix="/keys", tags=["API Keys"])


async def _load_application(api_key, session: AsyncSession):
    """Load the ClientApplication ORM object for the authenticated key."""
    from app.repositories.application_repo import ApplicationRepository
    from app.core.exceptions import ResourceNotFoundError

    repo = ApplicationRepository(session)
    try:
        return await repo.get_by_id(str(api_key.application_id))
    except (ResourceNotFoundError, Exception):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Application not found.",
        )


@router.get(
    "",
    status_code=status.HTTP_200_OK,
    summary="List all API keys for this application",
)
async def list_keys(
    request: Request,
    api_key=Depends(get_api_key),
    auth_svc=Depends(get_auth_service),
    session: AsyncSession = Depends(get_db),
):
    """Return all keys (active and revoked) for the authenticated application."""
    application = await _load_application(api_key, session)
    # Real signature: list_api_keys(*, application: ClientApplication)
    keys = await auth_svc.list_api_keys(application=application)
    return ApiResponse.ok(
        KeyListResponse.from_keys(keys),
        request_id=request.state.request_id,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a new API key",
)
async def create_key(
    request: Request,
    body: CreateKeyRequest,
    api_key=Depends(get_api_key),
    auth_svc=Depends(get_auth_service),
    session: AsyncSession = Depends(get_db),
):
    """
    Create a new API key for the authenticated application.
    raw_key is returned exactly once — store it immediately.
    """
    application = await _load_application(api_key, session)
    # Real signature: create_api_key(*, application, key_type, scopes, name, expires_at=None)
    new_key, raw_key = await auth_svc.create_api_key(
        application=application,
        name=body.name,
        key_type=body.key_type,
        scopes=body.scopes,
    )
    return ApiResponse.ok(
        CreateApiKeyResponse.from_service(new_key, raw_key),
        request_id=request.state.request_id,
    )


@router.delete(
    "/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key",
)
async def revoke_key(
    key_id: str,
    api_key=Depends(get_api_key),
    auth_svc=Depends(get_auth_service),
    session: AsyncSession = Depends(get_db),
):
    """Permanently revoke an API key."""
    application = await _load_application(api_key, session)
    # Real signature: revoke_api_key(*, key_id: UUID, application: ClientApplication)
    await auth_svc.revoke_api_key(
        key_id=UUID(key_id),
        application=application,
    )
    return None  # 204 No Content


@router.post(
    "/{key_id}/rotate",
    status_code=status.HTTP_200_OK,
    summary="Rotate an API key",
)
async def rotate_key(
    request: Request,
    key_id: str,
    api_key=Depends(get_api_key),
    auth_svc=Depends(get_auth_service),
    session: AsyncSession = Depends(get_db),
):
    """Revoke an existing key and issue a replacement with the same scopes."""
    application = await _load_application(api_key, session)
    # Real signature: rotate_api_key(*, key_id: UUID, application: ClientApplication)
    new_key, raw_key = await auth_svc.rotate_api_key(
        key_id=UUID(key_id),
        application=application,
    )
    return ApiResponse.ok(
        CreateApiKeyResponse.from_service(new_key, raw_key),
        request_id=request.state.request_id,
    )