"""
Monitoring and audit log response schemas.

Schema hierarchy:
  StatsOverviewResponse      — platform-wide aggregate stats (base)
  ServiceStatsResponse       — StatsOverviewResponse + service_type field
  DailyVolumeResponse        — call volume per calendar day
  AuditLogResponse           — single raw log entry
  MonitoringDashboardResponse — all four combined for the dashboard endpoint

Inheritance:
  ServiceStatsResponse extends StatsOverviewResponse by adding service_type.
  Pydantic v2 model inheritance includes all parent fields in the child.
  Both have from_row() classmethods for direct construction from DB result rows.

error_rate_pct is float (not Decimal):
  Percentage values for display purposes — not for monetary arithmetic.
  Rounding to 2 decimal places in from_row() is sufficient.
  '4.76%' displayed in a chart does not require Decimal precision.

application_id in AuditLogResponse is str | None:
  Unauthenticated requests are logged with application_id=None —
  the request never reached authentication. A spike in null-application-id
  entries indicates authentication failures or bot probing.

MonitoringDashboardResponse:
  Returns all sub-datasets in one HTTP call to avoid the dashboard
  frontend making 4 sequential API calls per page render.
  The sub-schemas are also returned individually by their own endpoints
  for clients that need only one piece of data.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import ApiMeta, PaginationMeta


# ── Sub-schemas ────────────────────────────────────────────────────────────

class StatsOverviewResponse(BaseModel):
    """
    Platform-wide aggregate statistics over a rolling time window.

    Returned by GET /monitoring/stats/overview.

    total_calls:    all logged API calls in the window.
    error_count:    calls with status_code >= 400.
    avg_response_ms: arithmetic mean response time across all calls.
    unique_apps:    distinct application_id values (None excluded).
    error_rate_pct: (error_count / total_calls) × 100, rounded to 2dp.
                    0.0 when total_calls is 0 (no division by zero).
    """
    model_config = ConfigDict(frozen=True)

    total_calls: int = Field(description="Total API calls in the reporting window")
    error_count: int = Field(description="Calls with status code >= 400")
    avg_response_ms: float = Field(
        description="Mean response time in milliseconds"
    )
    unique_apps: int = Field(
        description="Distinct applications that made API calls"
    )
    error_rate_pct: float = Field(
        description="Error rate as a percentage (0.0–100.0), rounded to 2dp"
    )

    @classmethod
    def from_row(cls, row: dict) -> "StatsOverviewResponse":
        """
        Build from AuditRepository.get_overview_stats() result dict.

        Handles division-by-zero for error_rate_pct when total_calls is 0.
        Rounds float fields to 2 decimal places for clean display.
        """
        total = row.get("total_calls", 0) or 0
        errors = row.get("error_count", 0) or 0
        error_rate = round((errors / total * 100), 2) if total > 0 else 0.0
        return cls(
            total_calls=total,
            error_count=errors,
            avg_response_ms=round(float(row.get("avg_response_ms") or 0.0), 2),
            unique_apps=row.get("unique_apps", 0) or 0,
            error_rate_pct=error_rate,
        )


class ServiceStatsResponse(StatsOverviewResponse):
    """
    Per-service aggregate statistics — StatsOverviewResponse + service_type.

    Returned as items in GET /monitoring/stats/services.
    Each item corresponds to one service_type value from ApiCallLog.

    service_type: one of 'sms', 'payments', 'ussd', 'numbers',
                  'notifications', or None for internal/health endpoints.
    """
    model_config = ConfigDict(frozen=True)

    service_type: str = Field(
        description="Service name: sms, payments, ussd, numbers, notifications"
    )

    @classmethod
    def from_row(cls, row: dict) -> "ServiceStatsResponse":
        """Build from a result row in AuditRepository.get_stats_by_service()."""
        total = row.get("total_calls", 0) or 0
        errors = row.get("error_count", 0) or 0
        error_rate = round((errors / total * 100), 2) if total > 0 else 0.0
        return cls(
            service_type=str(row.get("service_type", "unknown")),
            total_calls=total,
            error_count=errors,
            avg_response_ms=round(float(row.get("avg_response_ms") or 0.0), 2),
            unique_apps=row.get("unique_apps", 0) or 0,
            error_rate_pct=error_rate,
        )


class DailyVolumeResponse(BaseModel):
    """
    Call volume for a single calendar day.

    Returned as items in GET /monitoring/stats/daily/{app_id}.

    day: ISO date string 'YYYY-MM-DD' (not datetime — timezone-ambiguous).
         Days with zero calls are absent from the list — the client fills
         gaps with zeros when rendering charts.

    error_count included alongside call_count so the client can compute
    a per-day error rate or overlay error bars on the volume chart.
    """
    model_config = ConfigDict(frozen=True)

    day: str = Field(description="Calendar day in YYYY-MM-DD format")
    call_count: int = Field(description="Total API calls on this day")
    error_count: int = Field(description="Error calls (status >= 400) on this day")

    @classmethod
    def from_row(cls, row: dict) -> "DailyVolumeResponse":
        """
        Build from AuditRepository.get_calls_per_day() result row.

        'day' may be a date object or string depending on the DB driver.
        Normalise to YYYY-MM-DD string regardless.
        """
        day_val = row.get("day", "")
        if hasattr(day_val, "isoformat"):
            day_str = day_val.isoformat()
        else:
            day_str = str(day_val)[:10]  # truncate datetime to date portion

        return cls(
            day=day_str,
            call_count=row.get("call_count", 0) or 0,
            error_count=row.get("error_count", 0) or 0,
        )


class AuditLogResponse(BaseModel):
    """
    Single raw API call log entry.

    Returned as items in GET /monitoring/logs (paginated).
    Also returned by GET /monitoring/trace/{request_id}.

    application_id is str | None:
      None for unauthenticated requests — the API key was missing,
      invalid, or the request failed before authentication completed.
      A pattern of None application_ids at high volume indicates
      authentication problems or external scanning/probing.

    ip_address is str | None:
      May be None if the request came through a proxy that strips
      the originating IP, or if IP logging is disabled for privacy.

    All timestamps as ISO 8601 strings — AuditMiddleware records UTC.
    """
    model_config = ConfigDict(from_attributes=True)

    id: str = Field(description="Log entry ID (BigInteger as string)")
    endpoint: str = Field(description="Request path (e.g. '/api/v1/sms/send')")
    method: str = Field(description="HTTP method: GET, POST, DELETE, PATCH")
    status_code: int = Field(description="HTTP response status code")
    response_time_ms: int = Field(description="Request processing time in milliseconds")
    service_type: str | None = Field(
        description="Service category: sms, payments, ussd, numbers, notifications, or null"
    )
    application_id: str | None = Field(
        description="Application UUID as string — null for unauthenticated requests"
    )
    request_id: str = Field(
        description="Unique request identifier from X-Request-ID header"
    )
    ip_address: str | None = Field(
        description="Client IP address — may be null if stripped by proxy"
    )
    is_sandbox: bool = Field(
        description="True if the request used a sandbox API key"
    )
    created_at: str = Field(description="Request timestamp (ISO 8601 UTC)")

    @classmethod
    def from_orm(cls, log) -> "AuditLogResponse":
        """Build from ApiCallLog ORM instance."""
        return cls(
            id=str(log.id),
            endpoint=log.endpoint,
            method=log.method,
            status_code=log.status_code,
            response_time_ms=log.response_time_ms,
            service_type=getattr(log, "service_type", None),
            application_id=str(log.application_id)
                if log.application_id else None,
            request_id=log.request_id,
            ip_address=getattr(log, "ip_address", None),
            is_sandbox=getattr(log, "is_sandbox", False),
            created_at=log.created_at.isoformat() if log.created_at else "",
        )


class AuditLogListResponse(BaseModel):
    """
    Paginated list of audit log entries for GET /monitoring/logs.
    """
    model_config = ConfigDict(frozen=True)

    success: bool = True
    items: list[AuditLogResponse]
    pagination: PaginationMeta
    meta: ApiMeta

    @classmethod
    def from_service(
        cls,
        items: list,
        *,
        paginated,
        request_id: str,
    ) -> "AuditLogListResponse":
        return cls(
            items=[AuditLogResponse.from_orm(log) for log in items],
            pagination=PaginationMeta.from_paginated_result(paginated),
            meta=ApiMeta.build(request_id),
        )


class MonitoringDashboardResponse(BaseModel):
    """
    Complete monitoring dashboard — all sub-datasets in one response.

    Returned by GET /monitoring/dashboard.
    Avoids 4 sequential frontend requests per page render.

    The same sub-schemas are returned by granular endpoints:
      GET /monitoring/stats/overview   → StatsOverviewResponse
      GET /monitoring/stats/services   → list[ServiceStatsResponse]
      GET /monitoring/stats/daily/{id} → list[DailyVolumeResponse]
      GET /monitoring/logs             → AuditLogListResponse

    days: the reporting window used for all stats in this response.
          Allows the client to label charts correctly without inferring
          the window from the data.

    recent_logs: the most recent 10 log entries across all applications.
                 For per-application logs use GET /monitoring/logs?app_id=...
    """
    model_config = ConfigDict(frozen=True)

    overview: StatsOverviewResponse = Field(
        description="Platform-wide aggregate stats for the reporting window"
    )
    by_service: list[ServiceStatsResponse] = Field(
        description="Per-service breakdown, ordered by call volume descending"
    )
    daily_volume: list[DailyVolumeResponse] = Field(
        description="Daily call volume for the reporting window, ordered by day ascending"
    )
    recent_logs: list[AuditLogResponse] = Field(
        description="Most recent 10 log entries across all applications"
    )
    days: int = Field(
        description="Reporting window in days (all stats computed over this window)"
    )
    meta: ApiMeta

    @classmethod
    def from_service(
        cls,
        *,
        overview_row: dict,
        service_rows: list[dict],
        daily_rows: list[dict],
        recent_log_orms: list,
        days: int,
        request_id: str,
    ) -> "MonitoringDashboardResponse":
        """
        Build complete dashboard from all four data sources.

        Called by the monitoring route handler after fetching all data:
            overview  = await audit_svc.get_overview_stats(days=days)
            services  = await audit_svc.get_stats_by_service(days=days)
            daily     = await audit_svc.get_calls_per_day(app_id=..., days=days)
            logs, _   = await audit_svc.get_recent_logs(limit=10)
            return MonitoringDashboardResponse.from_service(
                overview_row=overview,
                service_rows=services,
                daily_rows=daily,
                recent_log_orms=logs,
                days=days,
                request_id=request.state.request_id,
            )
        """
        return cls(
            overview=StatsOverviewResponse.from_row(overview_row),
            by_service=[ServiceStatsResponse.from_row(r) for r in service_rows],
            daily_volume=[DailyVolumeResponse.from_row(r) for r in daily_rows],
            recent_logs=[AuditLogResponse.from_orm(log) for log in recent_log_orms],
            days=days,
            meta=ApiMeta.build(request_id),
        )