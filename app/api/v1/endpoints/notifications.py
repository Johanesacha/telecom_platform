"""
Multi-channel notification dispatch endpoints.

SIGNATURE CORRECTIONS vs Claude's original:
  dispatch(*, channel, recipient, body, subject=None,
           idempotency_key=None, request_id=None)
    returns tuple[NotificationRecord | dict, bool]

  get_record(*, record_id: UUID)  ← keyword-only, UUID not str

Route order: /send, /history before /{notification_id}
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import PaginationParams, get_notification_service
from app.schemas.common import ApiResponse
from app.schemas.notification import (
    NotificationDispatchRequest,
    NotificationHistoryResponse,
    NotificationResponse,
)

router = APIRouter(prefix="/notifications", tags=["Notifications"])


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


@router.post(
    "/send",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Dispatch a notification",
)
async def dispatch_notification(
    request: Request,
    body: NotificationDispatchRequest,
    notif_svc=Depends(get_notification_service),
):
    """
    Dispatch a notification via SMS, EMAIL, or PUSH.
    202 Accepted: record created and delivery queued.
    """
    # Real signature: dispatch(*, channel, recipient, body, subject=None,
    #                           idempotency_key=None, request_id=None)
    # Returns: tuple[NotificationRecord | dict, bool]
    record, _is_duplicate = await notif_svc.dispatch(
        channel=body.channel,
        recipient=body.recipient,
        body=body.body,
        subject=getattr(body, "subject", None),
        idempotency_key=getattr(body, "idempotency_key", None),
        request_id=getattr(request.state, "request_id", None),
    )
    return ApiResponse.ok(
        NotificationResponse.from_orm(record),
        request_id=request.state.request_id,
    )


@router.get(
    "/history",
    status_code=status.HTTP_200_OK,
    summary="List notifications (paginated)",
)
async def list_notifications(
    request: Request,
    pagination: PaginationParams = Depends(),
    notif_svc=Depends(get_notification_service),
):
    """Return paginated notification history."""
    items, total = await notif_svc.list_history(
        skip=pagination.skip,
        limit=pagination.limit,
    )
    paginated = _build_paginated(total, pagination)
    return NotificationHistoryResponse.from_service(
        items=items,
        paginated=paginated,
        request_id=request.state.request_id,
    )


@router.get(
    "/{notification_id}",
    status_code=status.HTTP_200_OK,
    summary="Get notification status",
)
async def get_notification(
    request: Request,
    notification_id: str,
    notif_svc=Depends(get_notification_service),
):
    """Return the delivery status of a specific notification."""
    # Real method: get_record(*, record_id: UUID)
    record = await notif_svc.get_record(record_id=UUID(notification_id))
    return ApiResponse.ok(
        NotificationResponse.from_orm(record),
        request_id=request.state.request_id,
    )