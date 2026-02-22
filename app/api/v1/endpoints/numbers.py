"""
MSISDN (phone number) verification endpoints.

POST /numbers/verify   → verify a number (200 — synchronous)
GET  /numbers/history  → paginated verification history

NumberService implements a three-tier cache:
  Tier 1: Redis (5-minute TTL) — from_cache=True, no quota consumed
  Tier 2: Recent DB record     — from_cache=True, no quota consumed
  Tier 3: Full verification    — from_cache=False, quota consumed

Real signatures:
  verify(*, raw_msisdn, country_hint='SN', request_id=None) -> dict
  list_verifications(*, skip, limit, operator_filter, valid_only)
    -> tuple[list[NumberVerification], int]
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import PaginationParams, get_number_service
from app.schemas.common import ApiResponse
from app.schemas.number import (
    NumberVerifyHistoryResponse,
    NumberVerifyRequest,
    NumberVerifyResponse,
)

router = APIRouter(prefix="/numbers", tags=["Number Verification"])


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
    "/verify",
    status_code=status.HTTP_200_OK,
    summary="Verify a phone number (MSISDN)",
)
async def verify_number(
    request: Request,
    body: NumberVerifyRequest,
    number_svc=Depends(get_number_service),
):
    """
    Verify whether an MSISDN is valid and active.

    Checks the three-tier cache before performing a full verification.
    from_cache=true in the response means no quota was consumed.
    """
    result = await number_svc.verify(
        raw_msisdn=body.msisdn,
        country_hint=getattr(body, "country_hint", "SN"),
        request_id=getattr(request.state, "request_id", None),
    )
    return ApiResponse.ok(
        NumberVerifyResponse.from_service(result),
        request_id=request.state.request_id,
    )


@router.get(
    "/history",
    status_code=status.HTTP_200_OK,
    summary="List number verifications (paginated)",
)
async def list_verifications(
    request: Request,
    pagination: PaginationParams = Depends(),
    number_svc=Depends(get_number_service),
):
    """Return paginated verification history for the authenticated application."""
    items, total = await number_svc.list_verifications(
        skip=pagination.skip,
        limit=pagination.limit,
    )
    paginated = _build_paginated(total, pagination)
    return NumberVerifyHistoryResponse.from_service(
        items=items,
        paginated=paginated,
        request_id=request.state.request_id,
    )