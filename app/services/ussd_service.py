"""
USSDService — orchestrates dual-storage USSD session lifecycle.

Redis   → authoritative session liveness, TTL enforcement
Postgres → permanent audit record, analytics, billing

EVERY operation touches BOTH stores. Ordering is strict:
  Start:   Redis write → Postgres write → commit
  Advance: Redis read (gating) → Redis write → Postgres write → commit
  End:     Redis delete → Postgres update → commit
  Cleanup: Postgres read → Postgres bulk update → commit (Redis already gone)

This service does not validate menu transitions or session semantics.
It validates that the session exists, is active, and the TTL has not
elapsed. Application-specific menu logic is the caller's responsibility.
"""
from __future__ import annotations

import json
import secrets
from datetime import timedelta
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ResourceNotFoundError,
    USSDSessionExpiredError,
    USSDSessionNotFoundError,
)
from app.domain.api_key import ApiKey, KeyType
from app.domain.ussd import USSDSession, USSDState
from app.repositories.ussd_repo import USSDRepository
from app.utils.time_utils import utcnow


_SESSION_TTL_SECONDS: int = 180
_REDIS_KEY_PREFIX_LIVE = "ussd:live"
_REDIS_KEY_PREFIX_SAND = "ussd:sand"
_SERVICE_NAME = "ussd"


class USSDService:
    """
    Manages USSD session lifecycle across Redis and PostgreSQL.

    Instantiate per-request with session, redis, and api_key.

    Usage in route handler:
        svc = USSDService(db, redis, api_key)
        ussd_session = await svc.start_session(
            msisdn="+221771234567",
            service_code="*144#",
        )
    """

    def __init__(
        self,
        session: AsyncSession,
        redis: aioredis.Redis,
        api_key: ApiKey,
    ) -> None:
        self._session = session
        self._redis = redis
        self._api_key = api_key
        self._app_id: UUID = api_key.application_id
        self._is_sandbox: bool = api_key.key_type == KeyType.SANDBOX
        self._repo = USSDRepository(session)

    # ── Session Lifecycle ──────────────────────────────────────────────────

    async def start_session(
        self,
        *,
        msisdn: str,
        service_code: str,
        initial_step: str = "MAIN_MENU",
        request_id: str | None = None,
    ) -> USSDSession:
        """
        Start a new USSD session.

        Creates the Redis state entry first. If the Postgres write fails,
        the Redis entry is cleaned up so no orphaned Redis key remains.

        Returns:
            The created USSDSession Postgres record.
        """
        session_id = secrets.token_hex(16)
        now = utcnow()
        expires_at = (now + timedelta(seconds=_SESSION_TTL_SECONDS)).replace(tzinfo=None)

        initial_state = {
            "session_id": session_id,
            "msisdn": msisdn,
            "current_step": initial_step,
            "step_history": [],
            "session_data": {},
            "app_id": str(self._app_id),
            "is_sandbox": self._is_sandbox,
        }

        redis_key = self._redis_key(session_id)

        # Write Redis first — gating condition for all subsequent operations
        await self._redis.setex(
            redis_key,
            _SESSION_TTL_SECONDS,
            json.dumps(initial_state),
        )

        try:
            ussd_session = await self._repo.create(
                application_id=self._app_id,
                session_id=session_id,
                msisdn=msisdn,
                current_step=initial_step,
                session_data={},
                state=USSDState.ACTIVE,
                expires_at=expires_at,
                is_sandbox=self._is_sandbox,
            )
            await self._session.commit()

        except Exception:
            # Postgres write failed — clean up the Redis entry
            await self._redis.delete(redis_key)
            raise

        return ussd_session

    async def advance_session(
        self,
        *,
        session_id: str,
        user_input: str,
        next_step: str,
        response_text: str,
        updated_session_data: dict | None = None,
    ) -> USSDSession:
        """
        Process one user input step and advance the session.

        Redis is the authoritative liveness check — if the key is absent,
        the session has expired regardless of the Postgres record's state.
        Resets the Redis TTL to 180 seconds from now on every advance.

        Raises:
            USSDSessionExpiredError:  Redis key absent — TTL elapsed.
            USSDSessionNotFoundError: Data inconsistency detected.
        """
        redis_key = self._redis_key(session_id)

        # Step 1: Redis read — authoritative liveness check
        raw_state = await self._redis.get(redis_key)
        if raw_state is None:
            raise USSDSessionExpiredError(
                f"USSD session '{session_id}' has expired. "
                f"Sessions expire after {_SESSION_TTL_SECONDS} seconds of inactivity."
            )

        current_state = json.loads(raw_state)
        now = utcnow()
        expires_at = (now + timedelta(seconds=_SESSION_TTL_SECONDS)).replace(tzinfo=None)
        new_expires_at = expires_at

        session_data = current_state.get("session_data", {})
        if updated_session_data:
            session_data = {**session_data, **updated_session_data}

        new_state = {
            **current_state,
            "current_step": next_step,
            "session_data": session_data,
            "step_history": current_state.get("step_history", []) + [
                {"step": current_state["current_step"], "input": user_input}
            ],
        }
        await self._redis.setex(
            redis_key,
            _SESSION_TTL_SECONDS,
            json.dumps(new_state),
        )

        db_session = await self._repo.get_active_by_session_id(session_id)
        if db_session is None:
            await self._redis.delete(redis_key)
            raise USSDSessionNotFoundError(
                f"Session '{session_id}' state is inconsistent. "
                f"Session has been terminated."
            )

        updated = await self._repo.advance_step(
            db_session,
            next_step=next_step,
            session_data=session_data,
            new_expires_at=new_expires_at,
        )
        await self._session.commit()
        return updated

    async def end_session(
        self,
        *,
        session_id: str,
    ) -> USSDSession:
        """
        Explicitly end an active USSD session.

        Deletes the Redis key and marks the Postgres record ENDED.

        Raises:
            USSDSessionNotFoundError: Session does not exist or already terminal.
        """
        redis_key = self._redis_key(session_id)

        db_session = await self._repo.get_active_by_session_id(session_id)
        if db_session is None:
            raise USSDSessionNotFoundError(
                f"USSD session '{session_id}' not found or already ended"
            )

        await self._redis.delete(redis_key)

        ended = await self._repo.mark_ended(db_session)
        await self._session.commit()
        return ended

    async def get_session(self, *, session_id: str) -> USSDSession:
        """
        Fetch the Postgres record for a USSD session.

        Raises:
            USSDSessionNotFoundError: No record with this session_id.
        """
        db_session = await self._repo.get_by_session_id(session_id)
        if db_session is None:
            raise USSDSessionNotFoundError(
                f"USSD session '{session_id}' not found"
            )
        return db_session

    async def list_sessions(
        self,
        *,
        skip: int = 0,
        limit: int = 20,
        state_filter: USSDState | None = None,
    ) -> tuple[list[USSDSession], int]:
        """Return paginated session history. Returns (items, total)."""
        total = await self._repo.count_by_application(
            self._app_id, state_filter=state_filter
        )
        items = await self._repo.list_by_application(
            self._app_id,
            skip=skip,
            limit=limit,
            state_filter=state_filter,
        )
        return items, total

    # ── Cleanup (called by Celery beat task) ───────────────────────────────

    async def cleanup_expired_sessions(
        self,
        *,
        batch_size: int = 100,
    ) -> int:
        """
        Mark one batch of expired ACTIVE sessions as TIMEOUT.

        Called by the Celery beat task in a loop until returns 0.
        Redis keys for these sessions are already gone (TTL evicted).

        Returns:
            Number of sessions marked TIMEOUT. 0 signals loop end.
        """
        now = utcnow()
        expired = await self._repo.get_expired_active_sessions(
            now, batch_size=batch_size
        )

        if not expired:
            return 0

        session_ids = [s.id for s in expired]
        count = await self._repo.bulk_mark_timed_out(session_ids)
        await self._session.commit()
        return count

    # ── Private Helpers ────────────────────────────────────────────────────

    def _redis_key(self, session_id: str) -> str:
        """
        Build the Redis key for a USSD session.

        Format:
          Live:    ussd:live:{session_id}
          Sandbox: ussd:sand:{session_id}
        """
        prefix = (
            _REDIS_KEY_PREFIX_SAND
            if self._is_sandbox
            else _REDIS_KEY_PREFIX_LIVE
        )
        return f"{prefix}:{session_id}"