"""
ApiCallLog repository — append-only audit and monitoring store.

Contracts:
  1. APPEND ONLY — never call update() or delete() on audit records.
  2. BigInteger PK — get_by_id() takes int, not UUID.
  3. Aggregate in SQL — never in Python.
  4. include_sandbox=False by default on all monitoring queries.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import distinct, func, select

from app.domain.audit import ApiCallLog
from app.repositories.base import BaseRepository


class AuditRepository(BaseRepository[ApiCallLog]):

    def __init__(self, session) -> None:
        super().__init__(ApiCallLog, session)

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
        return await self.create(
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

    async def get_by_request_id(self, request_id: str) -> ApiCallLog | None:
        stmt = select(ApiCallLog).where(ApiCallLog.request_id == request_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_overview_stats(self, *, days: int = 7, include_sandbox: bool = False) -> dict:
        since = self._days_ago(days)
        stmt = (
            select(
                func.count().label("total_calls"),
                func.count().filter(ApiCallLog.status_code >= 500).label("error_count"),
                func.coalesce(func.avg(ApiCallLog.response_time_ms), 0).label("avg_response_ms"),
                func.count(distinct(ApiCallLog.application_id)).label("unique_apps"),
            )
            .where(ApiCallLog.created_at >= since)
        )
        if not include_sandbox:
            stmt = stmt.where(ApiCallLog.is_sandbox.is_(False))
        result = await self.session.execute(stmt)
        row = result.mappings().one()
        return {
            "total_calls": row["total_calls"],
            "error_count": row["error_count"],
            "avg_response_ms": round(float(row["avg_response_ms"]), 2),
            "unique_apps": row["unique_apps"],
            "error_rate_pct": round((row["error_count"] / row["total_calls"] * 100) if row["total_calls"] > 0 else 0.0, 2),
        }

    async def get_stats_by_service(self, *, days: int = 7, include_sandbox: bool = False) -> list[dict]:
        since = self._days_ago(days)
        stmt = (
            select(
                ApiCallLog.service_type,
                func.count().label("call_count"),
                func.count().filter(ApiCallLog.status_code >= 500).label("error_count"),
                func.coalesce(func.avg(ApiCallLog.response_time_ms), 0).label("avg_response_ms"),
            )
            .where(ApiCallLog.created_at >= since, ApiCallLog.service_type.is_not(None))
            .group_by(ApiCallLog.service_type)
            .order_by(func.count().desc())
        )
        if not include_sandbox:
            stmt = stmt.where(ApiCallLog.is_sandbox.is_(False))
        result = await self.session.execute(stmt)
        return [
            {
                "service_type": row["service_type"],
                "call_count": row["call_count"],
                "error_count": row["error_count"],
                "avg_response_ms": round(float(row["avg_response_ms"]), 2),
                "error_rate_pct": round((row["error_count"] / row["call_count"] * 100) if row["call_count"] > 0 else 0.0, 2),
            }
            for row in result.mappings().all()
        ]

    async def get_stats_by_application(self, app_id: UUID, *, days: int = 7, include_sandbox: bool = False) -> dict:
        since = self._days_ago(days)
        stmt = (
            select(
                func.count().label("total_calls"),
                func.count().filter(ApiCallLog.status_code >= 500).label("error_count"),
                func.count().filter(ApiCallLog.status_code >= 400, ApiCallLog.status_code < 500).label("client_error_count"),
                func.coalesce(func.avg(ApiCallLog.response_time_ms), 0).label("avg_response_ms"),
                func.coalesce(func.max(ApiCallLog.response_time_ms), 0).label("max_response_ms"),
            )
            .where(ApiCallLog.application_id == app_id, ApiCallLog.created_at >= since)
        )
        if not include_sandbox:
            stmt = stmt.where(ApiCallLog.is_sandbox.is_(False))
        result = await self.session.execute(stmt)
        row = result.mappings().one()
        return {
            "total_calls": row["total_calls"],
            "error_count": row["error_count"],
            "client_error_count": row["client_error_count"],
            "avg_response_ms": round(float(row["avg_response_ms"]), 2),
            "max_response_ms": row["max_response_ms"],
            "error_rate_pct": round((row["error_count"] / row["total_calls"] * 100) if row["total_calls"] > 0 else 0.0, 2),
        }

    async def get_calls_per_day(self, app_id: UUID, *, days: int = 30, include_sandbox: bool = False) -> list[dict]:
        since = self._days_ago(days)
        stmt = (
            select(
                func.date_trunc("day", ApiCallLog.created_at).label("day"),
                func.count().label("call_count"),
                func.count().filter(ApiCallLog.status_code >= 500).label("error_count"),
            )
            .where(ApiCallLog.application_id == app_id, ApiCallLog.created_at >= since)
            .group_by(func.date_trunc("day", ApiCallLog.created_at))
            .order_by(func.date_trunc("day", ApiCallLog.created_at).asc())
        )
        if not include_sandbox:
            stmt = stmt.where(ApiCallLog.is_sandbox.is_(False))
        result = await self.session.execute(stmt)
        return [{"day": row.day.date().isoformat(), "call_count": row.call_count, "error_count": row.error_count} for row in result.all()]

    async def list_recent(self, *, app_id: UUID | None = None, service_type: str | None = None, status_code_gte: int | None = None, skip: int = 0, limit: int = 50, include_sandbox: bool = False) -> list[ApiCallLog]:
        effective_limit = min(limit, 200)
        stmt = select(ApiCallLog).order_by(ApiCallLog.created_at.desc()).offset(skip).limit(effective_limit)
        if app_id is not None:
            stmt = stmt.where(ApiCallLog.application_id == app_id)
        if service_type is not None:
            stmt = stmt.where(ApiCallLog.service_type == service_type)
        if status_code_gte is not None:
            stmt = stmt.where(ApiCallLog.status_code >= status_code_gte)
        if not include_sandbox:
            stmt = stmt.where(ApiCallLog.is_sandbox.is_(False))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_recent(self, *, app_id: UUID | None = None, service_type: str | None = None, status_code_gte: int | None = None, include_sandbox: bool = False) -> int:
        stmt = select(func.count()).select_from(ApiCallLog)
        if app_id is not None:
            stmt = stmt.where(ApiCallLog.application_id == app_id)
        if service_type is not None:
            stmt = stmt.where(ApiCallLog.service_type == service_type)
        if status_code_gte is not None:
            stmt = stmt.where(ApiCallLog.status_code >= status_code_gte)
        if not include_sandbox:
            stmt = stmt.where(ApiCallLog.is_sandbox.is_(False))
        result = await self.session.execute(stmt)
        return result.scalar_one()

    @staticmethod
    def _days_ago(days: int) -> datetime:
        from app.utils.time_utils import utcnow
        return utcnow() - timedelta(days=days)