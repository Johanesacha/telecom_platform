"""
Monitoring and audit log endpoints.

SIGNATURE CORRECTIONS vs Claude's original:
  get_calls_per_day(*, application_id: UUID, days, include_sandbox)
    Note: application_id= (UUID), not app_id=

  get_recent_logs(*, application_id: UUID | None, service_type, status_code_gte,
                  skip, limit, include_sandbox)
    Note: application_id= (UUID), not app_id=
    Note: status_code_gte (int | None), not status_code_gte with ge=100 default

  get_stats_by_application(*, application_id: UUID, days, include_sandbox)
"""
from __future__ import annotations

from uuid import UUID
from typing import List

from pydantic import BaseModel
from fastapi import APIRouter, Depends, Query, Request, status

from app.api.deps import PaginationParams, get_api_key, get_audit_service
from app.schemas.common import ApiResponse
from app.schemas.monitoring import (
    AuditLogListResponse,
    MonitoringDashboardResponse,
    ServiceStatsResponse,
    StatsOverviewResponse,
)

router = APIRouter(prefix="/monitoring", tags=["Monitoring"])


class _ServiceStatsList(BaseModel):
    items: List[ServiceStatsResponse]
    count: int


def _build_paginated(total: int, params: PaginationParams):
    from dataclasses import dataclass

    @dataclass
    class _P:
        total: int
        page: int
        pages: int
        limit: int
        skip: int
        has_next: bool
        has_prev: bool

    pages = max(1, (total + params.page_size - 1) // params.page_size) if total > 0 else 1
    return _P(
        total=total, page=params.page, pages=pages, limit=params.page_size,
        skip=params.skip, has_next=params.page < pages, has_prev=params.page > 1,
    )


@router.get(
    "/dashboard",
    status_code=status.HTTP_200_OK,
    summary="Complete monitoring dashboard",
)
async def get_dashboard(
    request: Request,
    days: int = Query(default=7, ge=1, le=90),
    include_sandbox: bool = Query(default=False),
    api_key=Depends(get_api_key),
    audit_svc=Depends(get_audit_service),
):
    """Return all monitoring sub-datasets in one response."""
    app_id = UUID(str(api_key.application_id))

    overview = await audit_svc.get_overview_stats(
        days=days, include_sandbox=include_sandbox
    )
    services = await audit_svc.get_stats_by_service(
        days=days, include_sandbox=include_sandbox
    )
    # Real signature: get_calls_per_day(*, application_id: UUID, ...)
    daily = await audit_svc.get_calls_per_day(
        application_id=app_id, days=days, include_sandbox=include_sandbox
    )
    # Real signature: get_recent_logs(*, application_id: UUID | None, ...)
    logs, _ = await audit_svc.get_recent_logs(
        application_id=app_id,
        service_type=None,
        status_code_gte=None,  # all status codes for dashboard
        skip=0,
        limit=10,
        include_sandbox=include_sandbox,
    )

    return MonitoringDashboardResponse.from_service(
        overview_row=overview,
        service_rows=services,
        daily_rows=daily,
        recent_log_orms=logs,
        days=days,
        request_id=request.state.request_id,
    )


@router.get(
    "/stats/overview",
    status_code=status.HTTP_200_OK,
    summary="Platform-wide aggregate statistics",
)
async def get_overview_stats(
    request: Request,
    days: int = Query(default=7, ge=1, le=90),
    include_sandbox: bool = Query(default=False),
    api_key=Depends(get_api_key),
    audit_svc=Depends(get_audit_service),
):
    overview = await audit_svc.get_overview_stats(
        days=days, include_sandbox=include_sandbox
    )
    return ApiResponse.ok(
        StatsOverviewResponse.from_row(overview),
        request_id=request.state.request_id,
    )


@router.get(
    "/stats/services",
    status_code=status.HTTP_200_OK,
    summary="Per-service statistics breakdown",
)
async def get_service_stats(
    request: Request,
    days: int = Query(default=7, ge=1, le=90),
    include_sandbox: bool = Query(default=False),
    api_key=Depends(get_api_key),
    audit_svc=Depends(get_audit_service),
):
    rows = await audit_svc.get_stats_by_service(
        days=days, include_sandbox=include_sandbox
    )
    items = [ServiceStatsResponse.from_row(r) for r in rows]
    return ApiResponse.ok(
        _ServiceStatsList(items=items, count=len(items)),
        request_id=request.state.request_id,
    )


@router.get(
    "/logs",
    status_code=status.HTTP_200_OK,
    summary="Paginated audit log entries",
)
async def get_audit_logs(
    request: Request,
    service_type: str | None = Query(default=None),
    status_code_gte: int = Query(default=400, ge=100, le=599),
    include_sandbox: bool = Query(default=False),
    pagination: PaginationParams = Depends(),
    api_key=Depends(get_api_key),
    audit_svc=Depends(get_audit_service),
):
    """Paginated audit log — default filter: status_code >= 400 (errors only)."""
    app_id = UUID(str(api_key.application_id))
    # Real signature: get_recent_logs(*, application_id: UUID | None, ...)
    items, total = await audit_svc.get_recent_logs(
        application_id=app_id,
        service_type=service_type,
        status_code_gte=status_code_gte,
        skip=pagination.skip,
        limit=pagination.limit,
        include_sandbox=include_sandbox,
    )
    paginated = _build_paginated(total, pagination)
    return AuditLogListResponse.from_service(
        items=items,
        paginated=paginated,
        request_id=request.state.request_id,
    )