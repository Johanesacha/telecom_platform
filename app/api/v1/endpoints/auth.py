"""
Authentication and developer application registration endpoints.

All routes use get_auth_service() — no API key required.
Auth routes are pre-authentication by definition.

SIGNATURE CORRECTIONS vs Claude's original:
  login()         → plain_password= (not password=)
  refresh_tokens()→ refresh_tokens (not refresh_token — note the 's')
  logout()        → takes user= object (User ORM), not a token string
                    logout() requires loading the User from DB first.
"""
from __future__ import annotations

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_auth_service, get_db
from app.schemas.auth import (
    RegisterApplicationRequest,
    RegisterApplicationResponse,
    RefreshRequest,
    TokenResponse,
)
from app.schemas.common import ApiResponse

router = APIRouter(prefix="/auth", tags=["Authentication"])


class _MessageResponse(BaseModel):
    message: str


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    summary="Register a new developer application",
)
async def register_application(
    request: Request,
    body: RegisterApplicationRequest,
    auth_svc=Depends(get_auth_service),
):
    """
    Create a new developer application.
    Returns LIVE and SANDBOX API keys — shown exactly once.
    """
    application, raw_live_key, raw_sandbox_key = await auth_svc.register_application(
        name=body.name,
        owner_email=str(body.owner_email),
        description=body.description,
    )
    return ApiResponse.ok(
        RegisterApplicationResponse.from_service(application, raw_live_key, raw_sandbox_key),
        request_id=request.state.request_id,
    )


@router.post(
    "/token",
    status_code=status.HTTP_200_OK,
    summary="Obtain JWT access token (OAuth2 password flow)",
)
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    auth_svc=Depends(get_auth_service),
):
    """
    Authenticate with email + password via OAuth2 password flow.
    Content-Type MUST be application/x-www-form-urlencoded.
    Fields: username (email address), password.
    """
    # Real signature: login(*, email, plain_password) → dict
    token_data = await auth_svc.login(
        email=form_data.username,
        plain_password=form_data.password,  # ← plain_password not password
    )
    resp = (
        token_data
        if isinstance(token_data, TokenResponse)
        else TokenResponse(**token_data)
    )
    return ApiResponse.ok(resp, request_id=request.state.request_id)


@router.post(
    "/refresh",
    status_code=status.HTTP_200_OK,
    summary="Refresh JWT access token",
)
async def refresh_token(
    request: Request,
    body: RefreshRequest,
    auth_svc=Depends(get_auth_service),
):
    """Exchange a valid refresh token for a new access + refresh token pair."""
    # Real method name: refresh_tokens (with 's') → dict
    token_data = await auth_svc.refresh_tokens(
        refresh_token=body.refresh_token,
    )
    resp = (
        token_data
        if isinstance(token_data, TokenResponse)
        else TokenResponse(**token_data)
    )
    return ApiResponse.ok(resp, request_id=request.state.request_id)


@router.post(
    "/logout",
    status_code=status.HTTP_200_OK,
    summary="Invalidate refresh token",
)
async def logout(
    request: Request,
    body: RefreshRequest,
    auth_svc=Depends(get_auth_service),
    session: AsyncSession = Depends(get_db),
):
    """
    Invalidate the provided refresh token.

    logout() requires a User ORM object — we load the user via
    AuthService.get_application_by_owner_email is not available here,
    so we look up by refresh token via auth_svc.refresh_tokens first
    to get the user, then call logout(user=user).

    Simpler approach: use refresh_tokens() to validate the token,
    get the user from the returned data, then logout.
    We use a direct user lookup via the token's email claim.
    """
    from app.repositories.user_repo import UserRepository
    import jwt as pyjwt
    from app.core.config import settings

    # Decode token to get email (don't verify expiry — that's logout's job)
    try:
        payload = pyjwt.decode(
            body.refresh_token,
            settings.jwt_private_key_path,  # sera corrigé plus tard
            algorithms=[settings.jwt_algorithm],
        )
        email = payload.get("sub")
        if not email:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token.",
            )
    except pyjwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token.",
        )

    user_repo = UserRepository(session)
    user = await user_repo.get_active_by_email(email)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found.",
        )

    # Real signature: logout(*, user: User) → None
    await auth_svc.logout(user=user)
    return ApiResponse.ok(
        _MessageResponse(message="Logged out successfully. Discard your access token."),
        request_id=request.state.request_id,
    )