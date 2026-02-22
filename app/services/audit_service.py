# app/services/audit_service.py

"""
AuditService — thin orchestration layer over AuditRepository.

Two consumers:
  AuditMiddleware        -> log_request() after every HTTP response
  Monitoring endpoints   -> get_overview_stats(), get_stats_by_service(),
                           get_stats_by_application(), get_recent_logs()

Design constraints:
  - No rate limiting — audit is internal, never billable
  - No authentication context required — logs unauthenticated requests too
  - log_request() commits its own session — middleware cannot share the
    request session which may already be closed or rolled back
  - All stat methods read-only — no writes beyond log_request()
  - Append-only by contract — no update or delete methods exposed
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.audit import ApiCallLog
from app.repositories.audit_repo import AuditRepository


class AuditService:
    """
    Records and queries API call audit logs.

    Instantiated in two contexts:

    Context 1 — AuditMiddleware (per-request logging):
        svc = AuditService(fresh_session)
        await svc.log_request(
            endpoint="/api/v1/sms/send",
            method="POST",
            status_code=202,
            response_time_ms=47,
            request_id="uuid-...",
            application_id=api_key.application_id,
            service_type="sms",
        )
        # AuditService.log_request() commits internally

    Context 2 — Monitoring route handlers:
        svc = AuditService(db)  # db from Depends(get_db)
        stats = await svc.get_overview_stats(days=7)
        # No commit needed — read-only queries
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = AuditRepository(session)

    # -- Write Path — called by AuditMiddleware ----------------------------

    async def log_request(
        self,
        *,
        endpoint: str,
        method: str,
        status_code: int,
        response_time_ms: int,
        request_id: str,
        application_id: UUID | None = None,
        service_type: str | None = None,
        ip_address: str | None = None,
        is_sandbox: bool = False,
    ) -> ApiCallLog:
        """
        Append one API call log record and commit.

        All parameters are keyword-only to prevent positional mistakes
        on a function called with many similar-typed arguments.

        Commits its own session. This is the correct design because:
          1. AuditMiddleware runs after the request session is closed
          2. The log record must be persisted even if the request failed
          3. A shared session would risk rolling back the log record
             when the request transaction rolls back

        Returns the created ApiCallLog for testability.
        """
        record = await self._repo.log_request(
            endpoint=endpoint,
            method=method,
            status_code=status_code,
            response_time_ms=response_time_ms,
            request_id=request_id,
            application_id=application_id,
            service_type=service_type,
            ip_address=ip_address,
            is_sandbox=is_sandbox,
        )
        await self._session.commit()
        return record

    # -- Read Path — called by monitoring route handlers -------------------

    async def get_overview_stats(
        self,
        *,
        days: int = 7,
        include_sandbox: bool = False,
    ) -> dict:
        """
        Return platform-wide summary statistics.

        Keys: total_calls, error_count, avg_response_ms,
              unique_apps, error_rate_pct

        Sandbox traffic excluded by default.
        """
        return await self._repo.get_overview_stats(
            days=days,
            include_sandbox=include_sandbox,
        )

    async def get_stats_by_service(
        self,
        *,
        days: int = 7,
        include_sandbox: bool = False,
    ) -> list[dict]:
        """
        Return aggregate statistics grouped by service type.

        Each item: service_type, call_count, error_count,
                   avg_response_ms, error_rate_pct

        Ordered by call_count DESC. Null service_type rows excluded.
        """
        return await self._repo.get_stats_by_service(
            days=days,
            include_sandbox=include_sandbox,
        )

    async def get_stats_by_application(
        self,
        *,
        application_id: UUID,
        days: int = 7,
        include_sandbox: bool = False,
    ) -> dict:
        """
        Return aggregate statistics for a single application.

        Keys: total_calls, error_count, client_error_count,
              avg_response_ms, max_response_ms, error_rate_pct
        """
        return await self._repo.get_stats_by_application(
            application_id,
            days=days,
            include_sandbox=include_sandbox,
        )

    async def get_calls_per_day(
        self,
        *,
        application_id: UUID,
        days: int = 30,
        include_sandbox: bool = False,
    ) -> list[dict]:
        """
        Return daily call volume for an application.

        Each item: {"day": "2026-02-01", "call_count": 847, "error_count": 3}

        Days with zero calls are absent. Ordered by day ASC.
        """
        return await self._repo.get_calls_per_day(
            application_id,
            days=days,
            include_sandbox=include_sandbox,
        )

    async def get_recent_logs(
        self,
        *,
        application_id: UUID | None = None,
        service_type: str | None = None,
        status_code_gte: int | None = None,
        skip: int = 0,
        limit: int = 50,
        include_sandbox: bool = False,
    ) -> tuple[list[ApiCallLog], int]:
        """
        Return paginated raw log entries with optional filters.

        Returns (items, total) for pagination metadata construction.

        Hard limit of 200 records per page enforced in AuditRepository.
        """
        items = await self._repo.list_recent(
            app_id=application_id,
            service_type=service_type,
            status_code_gte=status_code_gte,
            skip=skip,
            limit=limit,
            include_sandbox=include_sandbox,
        )
        total = await self._repo.count_recent(
            app_id=application_id,
            service_type=service_type,
            status_code_gte=status_code_gte,
            include_sandbox=include_sandbox,
        )
        return items, total

    async def get_by_request_id(
        self,
        request_id: str,
    ) -> ApiCallLog | None:
        """
        Fetch a single log entry by request ID.

        Used by GET /monitoring/trace/{request_id}.
        Returns None if not found — route handler decides 404 or empty.
        """
        return await self._repo.get_by_request_id(request_id)