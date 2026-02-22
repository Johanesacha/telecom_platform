"""
USSD session lifecycle endpoints.

SIGNATURE CORRECTIONS vs Claude's original:
  start_session(*, msisdn, service_code, initial_step='MAIN_MENU', request_id=None)
  advance_session(*, session_id, user_input, next_step, response_text, updated_session_data=None)
  get_session(*, session_id)      ← keyword-only
  end_session(*, session_id)      ← keyword-only
  list_sessions(*, skip, limit, state_filter=None)

Route order: /start, /respond, /history before /{session_id}
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import PaginationParams, get_ussd_service
from app.schemas.common import ApiResponse
from app.schemas.ussd import (
    USSDAdvanceRequest,
    USSDHistoryResponse,
    USSDSessionResponse,
    USSDStartRequest,
)

router = APIRouter(prefix="/ussd", tags=["USSD"])


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
    "/start",
    status_code=status.HTTP_201_CREATED,
    summary="Start a new USSD session",
)
async def start_session(
    request: Request,
    body: USSDStartRequest,
    ussd_svc=Depends(get_ussd_service),
):
    """Initiate a new USSD session."""
    # Real signature: start_session(*, msisdn, service_code, initial_step, request_id)
    session = await ussd_svc.start_session(
        msisdn=body.msisdn,
        service_code=body.service_code,
        initial_step=getattr(body, "initial_step", "MAIN_MENU"),
        request_id=getattr(request.state, "request_id", None),
    )
    return ApiResponse.ok(
        USSDSessionResponse.from_orm(session),
        request_id=request.state.request_id,
    )


@router.post(
    "/respond",
    status_code=status.HTTP_200_OK,
    summary="Advance USSD session by one step",
)
async def advance_session(
    request: Request,
    body: USSDAdvanceRequest,
    ussd_svc=Depends(get_ussd_service),
):
    """Record subscriber input and advance to next step."""
    # Real signature: advance_session(*, session_id, user_input, next_step,
    #                                  response_text, updated_session_data=None)
    session = await ussd_svc.advance_session(
        session_id=body.session_id,
        user_input=body.user_input,
        next_step=body.next_step,
        response_text=body.response_text,
        updated_session_data=getattr(body, "updated_session_data", None),
    )
    return ApiResponse.ok(
        USSDSessionResponse.from_orm(session),
        request_id=request.state.request_id,
    )


@router.get(
    "/history",
    status_code=status.HTTP_200_OK,
    summary="List USSD sessions (paginated)",
)
async def list_sessions(
    request: Request,
    pagination: PaginationParams = Depends(),
    ussd_svc=Depends(get_ussd_service),
):
    """Return paginated USSD session history."""
    items, total = await ussd_svc.list_sessions(
        skip=pagination.skip,
        limit=pagination.limit,
    )
    paginated = _build_paginated(total, pagination)
    return USSDHistoryResponse.from_service(
        items=items,
        paginated=paginated,
        request_id=request.state.request_id,
    )


@router.get(
    "/{session_id}",
    status_code=status.HTTP_200_OK,
    summary="Get USSD session state",
)
async def get_session(
    request: Request,
    session_id: str,
    ussd_svc=Depends(get_ussd_service),
):
    """Return the current state of a USSD session."""
    # Real signature: get_session(*, session_id: str)
    session = await ussd_svc.get_session(session_id=session_id)
    return ApiResponse.ok(
        USSDSessionResponse.from_orm(session),
        request_id=request.state.request_id,
    )


@router.delete(
    "/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="End a USSD session",
)
async def end_session(
    session_id: str,
    ussd_svc=Depends(get_ussd_service),
):
    """Explicitly terminate a USSD session before TTL expiry."""
    # Real signature: end_session(*, session_id: str)
    await ussd_svc.end_session(session_id=session_id)
    return None