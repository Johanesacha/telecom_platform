"""
Quota and usage endpoints.

GET /quota/usage → current daily usage for all services.

QuotaService is constructed with (api_key, redis) and is already
scoped to the authenticated application. get_all_usage() returns
usage data without needing additional application_id arguments.
"""
from __future__ import annotations

from typing import List

from pydantic import BaseModel
from fastapi import APIRouter, Depends, Request, status

from app.api.deps import get_api_key, get_quota_service
from app.schemas.common import ApiResponse

router = APIRouter(prefix="/quota", tags=["Quota"])


class _ServiceUsage(BaseModel):
    """Usage data for one service."""
    service: str
    used_today: int
    daily_limit: int
    remaining: int
    reset_at: str


class _UsageSummary(BaseModel):
    """Complete quota usage summary for the authenticated application."""
    application_id: str
    plan: str
    services: List[_ServiceUsage]


def _parse_service_usage(name: str, data) -> _ServiceUsage:
    """
    Convert a single service usage dict or object to _ServiceUsage.

    Handles both dict and object-style returns from QuotaService.
    """
    if isinstance(data, dict):
        return _ServiceUsage(
            service=name,
            used_today=int(data.get("used_today", data.get("count", 0))),
            daily_limit=int(data.get("daily_limit", data.get("limit", 0))),
            remaining=int(data.get("remaining", 0)),
            reset_at=str(data.get("reset_at", data.get("resets_at", ""))),
        )
    # Object-style (has attributes)
    return _ServiceUsage(
        service=name,
        used_today=int(getattr(data, "used_today", getattr(data, "count", 0))),
        daily_limit=int(getattr(data, "daily_limit", getattr(data, "limit", 0))),
        remaining=int(getattr(data, "remaining", 0)),
        reset_at=str(getattr(data, "reset_at", getattr(data, "resets_at", ""))),
    )


@router.get(
    "/usage",
    status_code=status.HTTP_200_OK,
    summary="Get current daily quota usage",
)
async def get_quota_usage(
    request: Request,
    api_key=Depends(get_api_key),
    quota_svc=Depends(get_quota_service),
):
    """
    Return current daily quota usage for all services.

    Counters reset at midnight UTC.
    used_today reflects all calls made today including sandbox calls
    (sandbox calls consume sandbox quota, not live quota).
    """
    usage_data = await quota_svc.get_all_usage()

    plan = (
        api_key.plan.value
        if hasattr(api_key, "plan") and hasattr(api_key.plan, "value")
        else str(getattr(api_key, "plan", "FREE"))
    )

    services: list[_ServiceUsage] = []
    if isinstance(usage_data, dict):
        for svc_name, svc_data in usage_data.items():
            services.append(_parse_service_usage(svc_name, svc_data))
    elif isinstance(usage_data, (list, tuple)):
        for entry in usage_data:
            if isinstance(entry, dict):
                name = entry.get("service", entry.get("service_name", "unknown"))
                services.append(_parse_service_usage(name, entry))

    return ApiResponse.ok(
        _UsageSummary(
            application_id=str(api_key.application_id),
            plan=plan,
            services=services,
        ),
        request_id=request.state.request_id,
    )