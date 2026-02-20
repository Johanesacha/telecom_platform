"""
JWT token creation and verification using RS256 (asymmetric RSA).

Why RS256 over HS256:
  HS256 = symmetric (shared secret). Any verifier must know the secret.
  RS256 = asymmetric (private key signs, public key verifies).
  Future services can verify tokens with only the public key — no secret sharing.
  Eliminates algorithm confusion attacks (CVE-2015-9235 class).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from jose import JWTError, jwt

from app.core.config import settings
from app.core.exceptions import AuthenticationError
from app.utils.time_utils import utcnow, utcnow_plus


def create_access_token(subject: str, extra_claims: dict[str, Any] | None = None) -> str:
    """
    Create a short-lived JWT access token.

    Args:
        subject: The user ID (UUID as string) — stored in 'sub' claim.
        extra_claims: Additional claims (role, email, etc.)

    Returns:
        Signed JWT string.
    """
    now = utcnow()
    expire = utcnow_plus(minutes=settings.access_token_expire_minutes)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": expire,
        "type": "access",
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.jwt_private_key, algorithm="RS256")


def create_refresh_token(subject: str) -> str:
    """
    Create a long-lived refresh token.
    Stored as a hash on the User record — invalidated on rotation.
    """
    expire = utcnow_plus(days=settings.refresh_token_expire_days)
    payload = {
        "sub": subject,
        "exp": expire,
        "type": "refresh",
    }
    return jwt.encode(payload, settings.jwt_private_key, algorithm="RS256")


def verify_token(token: str, expected_type: str = "access") -> dict[str, Any]:
    """
    Verify a JWT token and return its payload.

    Validates:
      - Signature (RS256 with public key)
      - Expiration (exp claim)
      - Issued-at (iat claim)
      - Token type ('access' or 'refresh')

    Raises:
        AuthenticationError on any validation failure.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_public_key,
            algorithms=["RS256"],
        )
    except JWTError as e:
        raise AuthenticationError(f"Invalid token: {e}") from e

    if payload.get("type") != expected_type:
        raise AuthenticationError(
            f"Wrong token type. Expected {expected_type}, got {payload.get('type')}."
        )

    subject = payload.get("sub")
    if not subject:
        raise AuthenticationError("Token missing 'sub' claim")

    return payload