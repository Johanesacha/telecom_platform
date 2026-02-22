"""
SMS sending and status endpoints.

SIGNATURE CORRECTIONS vs Claude's original:
  send() takes individual kwargs, not a schema object:
    send(*, to_number, message_text, from_alias=None, idempotency_key=None, request_id=None)
    returns tuple[SMSMessage | dict, bool]

  get_message_status() not get_by_id():
    get_message_status(*, message_id: UUID)

Route order: /history and /send before /{sms_id}
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import PaginationParams, get_sms_service
from app.schemas.common import ApiResponse
from app.schemas.sms import (
    SMSHistoryResponse,
    SMSSendRequest,
    SMSSendResponse,
    SMSStatusResponse,
)

router = APIRouter(prefix="/sms", tags=["SMS"])


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
    summary="Send an SMS message",
)
async def send_sms(
    request: Request,
    body: SMSSendRequest,
    sms_svc=Depends(get_sms_service),
):
    """
    Submit an SMS for delivery.
    202 Accepted: message queued. Use GET /sms/{id} to track status.
    """
    # Real signature: send(*, to_number, message_text, from_alias=None,
    #                      idempotency_key=None, request_id=None)
    # Returns: tuple[SMSMessage | dict, bool]  (message, is_duplicate)
    message, _is_duplicate = await sms_svc.send(
        to_number=body.to_number,
        message_text=body.message_text,
        from_alias=body.from_alias,
        idempotency_key=body.idempotency_key,
        request_id=getattr(request.state, "request_id", None),
    )
    return ApiResponse.ok(
        SMSSendResponse.from_orm(message),
        request_id=request.state.request_id,
    )


@router.get(
    "/history",
    status_code=status.HTTP_200_OK,
    summary="List sent SMS messages (paginated)",
)
async def list_sms_history(
    request: Request,
    pagination: PaginationParams = Depends(),
    sms_svc=Depends(get_sms_service),
):
    """Return paginated SMS history for the authenticated application."""
    items, total = await sms_svc.list_history(
        skip=pagination.skip,
        limit=pagination.limit,
    )
    paginated = _build_paginated(total, pagination)
    return SMSHistoryResponse.from_service(
        items=items,
        paginated=paginated,
        request_id=request.state.request_id,
    )


@router.get(
    "/{sms_id}",
    status_code=status.HTTP_200_OK,
    summary="Get SMS delivery status",
)
async def get_sms_status(
    request: Request,
    sms_id: str,
    sms_svc=Depends(get_sms_service),
):
    """Return the current delivery status of a specific SMS message."""
    # Real method: get_message_status(*, message_id: UUID)
    message = await sms_svc.get_message_status(message_id=UUID(sms_id))
    return ApiResponse.ok(
        SMSStatusResponse.from_orm(message),
        request_id=request.state.request_id,
    )