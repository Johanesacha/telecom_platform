"""
AuthService — user registration, login, JWT lifecycle, API key management.

Three domains of responsibility:
  1. Human operator auth: User registration, login, token refresh, logout
  2. Developer registration: ClientApplication + ApiKey creation
  3. API key lifecycle: create, list, revoke, rotate

Commit policy:
  This service commits. It is the only layer that calls session.commit().
  Repositories flush only. The service decides transaction boundaries.

Security invariants maintained here:
  - Refresh token rotation: one valid refresh token per user at all times
  - Raw API keys: generated here, returned once, never stored or logged
  - Scope validation: keys cannot have scopes beyond their plan's entitlement
  - Timing consistency: same AuthenticationError for wrong email and wrong password
"""
from __future__ import annotations

import hashlib
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    AuthenticationError,
    InsufficientScopeError,
    InvalidApiKeyError,
    ResourceNotFoundError,
    RevokedApiKeyError,
)
from app.domain.api_key import ApiKey, KeyType
from app.domain.application import AppPlan, ClientApplication
from app.domain.user import User, UserRole
from app.repositories.api_key_repo import ApiKeyRepository
from app.repositories.application_repo import ApplicationRepository
from app.repositories.user_repo import UserRepository
from app.security.api_key import generate_api_key, verify_api_key
from app.security.jwt import create_access_token, create_refresh_token, verify_token
from app.security.password import hash_password, verify_password
from app.security.scopes import DEFAULT_SCOPES, Scope


# Maximum number of active (non-revoked, non-expired) keys per application.
# Prevents runaway key proliferation — 10 is generous for any real use case.
_MAX_KEYS_PER_APPLICATION: int = 10


class AuthService:
    """
    Coordinates authentication and credential lifecycle across repositories.

    Instantiate per-request with the async session. The session is shared
    across all repository instances so all flushes participate in the
    same transaction — one commit() at the end of each operation.

    Usage in route handlers:
        svc = AuthService(db)
        user, app, raw_key = await svc.register_application(...)
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._user_repo = UserRepository(session)
        self._app_repo = ApplicationRepository(session)
        self._key_repo = ApiKeyRepository(session)

    # ══════════════════════════════════════════════════════════════════════
    # HUMAN OPERATOR REGISTRATION & AUTH
    # ══════════════════════════════════════════════════════════════════════

    async def register_user(
        self,
        *,
        email: str,
        plain_password: str,
        full_name: str,
        role: UserRole = UserRole.MANAGER,
    ) -> User:
        """
        Create a new platform operator account (Manager or Admin).

        Not self-service — this must be called by an existing Admin
        via the admin endpoint. The route enforces this via
        Depends(require_role(UserRole.ADMIN)).

        Email is normalised to lowercase. The password is hashed with
        bcrypt (rounds=12, ~250ms) before storage. The plain password
        is never stored, logged, or returned.

        Raises:
            AuthenticationError: If the email is already registered.
                                 Uses AuthenticationError (not a conflict
                                 error) to prevent email enumeration via
                                 the admin endpoint.
        """
        normalised_email = email.lower().strip()

        if await self._user_repo.email_exists(normalised_email):
            raise AuthenticationError(
                "An account with this email already exists"
            )

        hashed = hash_password(plain_password)

        user = await self._user_repo.create(
            email=normalised_email,
            hashed_password=hashed,
            full_name=full_name,
            role=role,
            is_active=True,
            refresh_token_hash=None,
        )

        await self._session.commit()
        return user

    async def login(
        self,
        *,
        email: str,
        plain_password: str,
    ) -> dict:
        """
        Authenticate a human operator and issue a JWT token pair.

        The same AuthenticationError is raised for both 'email not found'
        and 'wrong password' cases. This prevents user enumeration:
        an attacker cannot determine whether an email is registered
        by observing which error they receive.

        verify_password() always runs even when the user is not found
        (using a dummy hash). This ensures the response time is identical
        whether the email exists or not — preventing timing-based
        enumeration attacks.

        Returns:
            dict with keys: access_token, refresh_token, token_type,
                           expires_in (seconds), user_id, role
        """
        normalised_email = email.lower().strip()
        user = await self._user_repo.get_active_by_email(normalised_email)

        # Always run verify_password, even when user is None.
        # This makes the response time identical for unknown email
        # and wrong password — prevents timing oracle.
        dummy_hash = hash_password("dummy_constant_prevents_timing_oracle")
        stored_hash = user.hashed_password if user is not None else dummy_hash
        password_correct = verify_password(plain_password, stored_hash)

        if user is None or not password_correct:
            raise AuthenticationError(
                "Invalid email or password"
            )

        access_token = create_access_token(
            subject=str(user.id),
            extra_claims={"role": user.role.value},
        )
        refresh_token = create_refresh_token(subject=str(user.id))

        token_hash = _sha256(refresh_token)
        await self._user_repo.store_refresh_token_hash(user, token_hash)

        await self._session.commit()

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "expires_in": 15 * 60,  # 15 minutes in seconds
            "user_id": str(user.id),
            "role": user.role.value,
        }

    async def refresh_tokens(self, *, refresh_token: str) -> dict:
        """
        Rotate a refresh token and issue a new token pair.

        Enforces the single-valid-token invariant:
          - The incoming refresh token is verified cryptographically
          - Its SHA-256 hash is compared against the stored hash
          - If they match: issue new pair, atomically replace stored hash
          - If they do not match: the token was already rotated
            (legitimate user already refreshed) or was never issued
            → raise AuthenticationError — the session is invalid

        After this call the incoming refresh_token is invalid.
        The caller must discard it and use only the returned tokens.
        """
        try:
            payload = verify_token(refresh_token, expected_type="refresh")
        except Exception:
            raise AuthenticationError(
                "Refresh token is invalid or expired"
            )

        try:
            user_id = UUID(payload["sub"])
        except (KeyError, ValueError):
            raise AuthenticationError("Malformed token payload")

        user = await self._user_repo.get_by_id(user_id)
        if user is None or not user.is_active:
            raise AuthenticationError("User account not found or deactivated")

        incoming_hash = _sha256(refresh_token)
        if not user.refresh_token_hash or \
                not _constant_time_compare(incoming_hash, user.refresh_token_hash):
            raise AuthenticationError(
                "Refresh token has already been rotated or revoked. "
                "Please log in again."
            )

        new_access = create_access_token(
            subject=str(user.id),
            extra_claims={"role": user.role.value},
        )
        new_refresh = create_refresh_token(subject=str(user.id))
        new_hash = _sha256(new_refresh)

        # Atomically replace old hash with new hash.
        # After commit, the old refresh_token is permanently invalid.
        await self._user_repo.rotate_refresh_token(user, new_hash)
        await self._session.commit()

        return {
            "access_token": new_access,
            "refresh_token": new_refresh,
            "token_type": "bearer",
            "expires_in": 15 * 60,
            "user_id": str(user.id),
            "role": user.role.value,
        }

    async def logout(self, *, user: User) -> None:
        """
        Invalidate the user's current refresh token.

        Sets refresh_token_hash to None. Any existing refresh token
        for this user can no longer be used to obtain new access tokens.
        The user must log in again with email and password.

        Access tokens already in circulation remain valid until their
        15-minute expiry. This is the documented trade-off for stateless
        JWT — no blacklist, 15-minute maximum exposure window on logout.
        """
        await self._user_repo.clear_refresh_token(user)
        await self._session.commit()

    async def change_user_role(
        self,
        *,
        user_id: UUID,
        new_role: UserRole,
        requesting_admin: User,
    ) -> User:
        """
        Change a platform operator's role.

        Prevents self-demotion: an Admin cannot change their own role.
        This ensures there is always at least one Admin — the last Admin
        cannot accidentally lock themselves out.

        Role changes take effect on the next login — existing access
        tokens carry the old role claim until expiry (max 15 minutes).
        """
        if user_id == requesting_admin.id:
            raise AuthenticationError(
                "Administrators cannot change their own role"
            )

        target_user = await self._user_repo.get_by_id(user_id)
        if target_user is None:
            raise ResourceNotFoundError(f"User {user_id} not found")

        updated = await self._user_repo.change_role(target_user, new_role)
        await self._session.commit()
        return updated

    async def deactivate_user(
        self,
        *,
        user_id: UUID,
        requesting_admin: User,
    ) -> User:
        """
        Deactivate a platform operator account.

        Prevents self-deactivation. Also clears the refresh token
        to immediately invalidate the session — the next refresh
        attempt will fail even before the access token expires.
        """
        if user_id == requesting_admin.id:
            raise AuthenticationError(
                "Administrators cannot deactivate their own account"
            )

        target_user = await self._user_repo.get_by_id(user_id)
        if target_user is None:
            raise ResourceNotFoundError(f"User {user_id} not found")

        await self._user_repo.clear_refresh_token(target_user)
        deactivated = await self._user_repo.deactivate(target_user)
        await self._session.commit()
        return deactivated

    # ══════════════════════════════════════════════════════════════════════
    # DEVELOPER APPLICATION REGISTRATION
    # ══════════════════════════════════════════════════════════════════════

    async def register_application(
        self,
        *,
        name: str,
        owner_email: str,
        description: str | None = None,
    ) -> tuple[ClientApplication, str, str]:
        """
        Register a new developer application and issue its initial key pair.

        Creates three records in a single transaction:
          1. ClientApplication (FREE plan)
          2. LIVE ApiKey (tp_live_...)
          3. SANDBOX ApiKey (tp_sand_...)

        Both raw key strings are returned exactly once. They are never
        stored, never logged, and never retrievable after this call.
        The caller must return them to the developer in the HTTP response
        and make clear they will not be shown again.

        Raises:
            AuthenticationError: If an application is already registered
                                 to this email address.

        Returns:
            (application, raw_live_key, raw_sandbox_key)
        """
        normalised_email = owner_email.lower().strip()

        existing = await self._app_repo.get_by_owner_email(normalised_email)
        if existing is not None:
            raise AuthenticationError(
                "An application is already registered to this email address. "
                "Log in to manage your existing application."
            )

        application = await self._app_repo.create(
            name=name,
            owner_email=normalised_email,
            plan=AppPlan.FREE,
            is_active=True,
            description=description,
        )

        # Generate LIVE key
        raw_live, live_prefix, live_hash = generate_api_key(sandbox=False)
        await self._key_repo.create(
            application_id=application.id,
            key_prefix=live_prefix,
            key_hash=live_hash,
            key_type=KeyType.LIVE,
            scopes=DEFAULT_SCOPES[AppPlan.FREE],
            name="Default Live Key",
            is_revoked=False,
            expires_at=None,
        )

        # Generate SANDBOX key
        raw_sand, sand_prefix, sand_hash = generate_api_key(sandbox=True)
        await self._key_repo.create(
            application_id=application.id,
            key_prefix=sand_prefix,
            key_hash=sand_hash,
            key_type=KeyType.SANDBOX,
            scopes=DEFAULT_SCOPES[AppPlan.FREE],
            name="Default Sandbox Key",
            is_revoked=False,
            expires_at=None,
        )

        # Single commit — application + both keys atomically.
        # Crash before commit: nothing persists, developer retries.
        # Crash after commit: all three records exist consistently.
        await self._session.commit()

        return application, raw_live, raw_sand

    # ══════════════════════════════════════════════════════════════════════
    # API KEY LIFECYCLE
    # ══════════════════════════════════════════════════════════════════════

    async def create_api_key(
        self,
        *,
        application: ClientApplication,
        key_type: KeyType,
        scopes: list[str],
        name: str,
        expires_at=None,
    ) -> tuple[ApiKey, str]:
        """
        Generate and store a new API key for an application.

        Validates that requested scopes are within the plan's entitlement.
        Enforces the per-application key limit (_MAX_KEYS_PER_APPLICATION).

        Returns:
            (ApiKey record, raw_key_string)
            The raw key string is returned once and never accessible again.

        Raises:
            InsufficientScopeError: Requested scopes exceed plan entitlement.
            AuthenticationError:    Active key limit reached.
        """
        self._validate_scopes_for_plan(scopes, application.plan)

        active_keys = await self._key_repo.get_active_by_application_id(
            application.id
        )
        if len(active_keys) >= _MAX_KEYS_PER_APPLICATION:
            raise AuthenticationError(
                f"Maximum of {_MAX_KEYS_PER_APPLICATION} active API keys "
                f"per application. Revoke an existing key before creating "
                f"a new one."
            )

        is_sandbox = key_type == KeyType.SANDBOX
        raw_key, key_prefix, key_hash = generate_api_key(sandbox=is_sandbox)

        api_key = await self._key_repo.create(
            application_id=application.id,
            key_prefix=key_prefix,
            key_hash=key_hash,
            key_type=key_type,
            scopes=scopes,
            name=name,
            is_revoked=False,
            expires_at=expires_at,
        )

        await self._session.commit()
        return api_key, raw_key

    async def revoke_api_key(
        self,
        *,
        key_id: UUID,
        application: ClientApplication,
    ) -> ApiKey:
        """
        Revoke an API key by ID, scoped to the owning application.

        The key_id must belong to the given application. If it belongs
        to a different application, ResourceNotFoundError is raised —
        not an authorisation error — to prevent confirming that a key
        with that ID exists in another application.

        The key is marked is_revoked=True. Subsequent authentication
        attempts with this key will fail at get_active_by_prefix()
        which filters on is_revoked=False.

        Raises:
            ResourceNotFoundError: Key not found or belongs to another app.
            AuthenticationError:   Key is already revoked.
        """
        api_key = await self._key_repo.get_by_id(key_id)

        if api_key is None or api_key.application_id != application.id:
            raise ResourceNotFoundError(
                f"API key {key_id} not found"
            )

        if api_key.is_revoked:
            raise RevokedApiKeyError(
                "This API key has already been revoked"
            )

        revoked = await self._key_repo.revoke_key(api_key)
        await self._session.commit()
        return revoked

    async def rotate_api_key(
        self,
        *,
        key_id: UUID,
        application: ClientApplication,
    ) -> tuple[ApiKey, str]:
        """
        Revoke an existing key and create a replacement in one transaction.

        The new key inherits the same key_type and scopes as the old key.
        This preserves the developer's intended configuration without
        requiring them to re-specify scopes during rotation.

        Atomicity guarantee: both revoke and create flush to the same
        transaction. A single commit() persists both. A crash before
        commit rolls back both — the old key remains valid, no orphaned
        new key exists.

        There is no window where the application has zero valid keys:
        the old key remains queryable (though marked revoked) until
        commit, and the new key is created in the same transaction.

        Returns:
            (new_ApiKey_record, new_raw_key_string)
        """
        old_key = await self._key_repo.get_by_id(key_id)

        if old_key is None or old_key.application_id != application.id:
            raise ResourceNotFoundError(f"API key {key_id} not found")

        if old_key.is_revoked:
            raise RevokedApiKeyError(
                "Cannot rotate a key that is already revoked. "
                "Create a new key instead."
            )

        is_sandbox = old_key.key_type == KeyType.SANDBOX
        new_raw, new_prefix, new_hash = generate_api_key(sandbox=is_sandbox)

        # Flush revoke and create in the same open transaction.
        # No commit between them — both are pending until the final commit.
        await self._key_repo.revoke_key(old_key)

        new_key = await self._key_repo.create(
            application_id=application.id,
            key_prefix=new_prefix,
            key_hash=new_hash,
            key_type=old_key.key_type,
            scopes=old_key.scopes,
            name=f"{old_key.name} (rotated)",
            is_revoked=False,
            expires_at=None,
        )

        await self._session.commit()
        return new_key, new_raw

    async def list_api_keys(
        self,
        *,
        application: ClientApplication,
    ) -> list[ApiKey]:
        """
        Return all API keys for an application — active and revoked.

        The full history is returned so developers can see their rotation
        audit trail. The route layer filters or labels revoked keys in
        the response schema — the service returns the complete set.
        """
        return await self._key_repo.get_by_application_id(application.id)

    async def get_application_by_owner_email(
        self,
        email: str,
    ) -> ClientApplication | None:
        """
        Look up an application by owner email.

        Used by management endpoints to find an application for a
        given developer email. Returns None if not found — the caller
        decides whether to raise ResourceNotFoundError.
        """
        return await self._app_repo.get_by_owner_email(email.lower().strip())

    # ══════════════════════════════════════════════════════════════════════
    # PRIVATE HELPERS
    # ══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _validate_scopes_for_plan(
        requested: list[str],
        plan: AppPlan,
    ) -> None:
        """
        Confirm all requested scopes are within the plan's entitlement.

        FREE plan: SMS_SEND, SMS_READ, NUMBERS_READ
        STANDARD:  + USSD_MANAGE, NOTIFICATIONS_SEND
        PREMIUM:   + PAYMENTS_WRITE, PAYMENTS_READ

        Raises InsufficientScopeError listing the disallowed scopes.
        """
        allowed = set(DEFAULT_SCOPES[plan])
        requested_set = set(requested)
        disallowed = requested_set - allowed

        if disallowed:
            raise InsufficientScopeError(
                f"The following scopes are not available on the "
                f"{plan.value} plan: {sorted(disallowed)}. "
                f"Upgrade your plan to access these scopes."
            )


# ── Module-level private helpers ───────────────────────────────────────────

def _sha256(value: str) -> str:
    """
    Return the SHA-256 hex digest of a UTF-8 encoded string.

    Used for refresh token hashing. The raw token is never stored —
    only this hash. Verification compares hashes, not raw values.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _constant_time_compare(a: str, b: str) -> bool:
    """
    Compare two strings in constant time.

    Prevents timing attacks where an attacker measures response time
    to determine how many characters of a token match the stored value.
    Uses hmac.compare_digest which is constant-time regardless of
    where the strings differ.
    """
    import hmac
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))