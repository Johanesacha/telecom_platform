"""
Mobile money payment endpoints.

SIGNATURE CORRECTIONS vs Claude's original:
  initiate() takes individual kwargs:
    initiate(*, payer_msisdn, receiver_msisdn, amount, currency, reference,
             idempotency_key=None, request_id=None, metadata=None)
    returns tuple[PaymentTransaction | dict, bool]

  get_transaction() not get_by_id():
    get_transaction(*, transaction_id: UUID)

Route order: /initiate, /history before /{payment_id}
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import PaginationParams, get_payment_service
from app.schemas.common import ApiResponse
from app.schemas.payment import (
    PaymentHistoryResponse,
    PaymentInitiateRequest,
    PaymentInitiateResponse,
    PaymentStatusResponse,
)

router = APIRouter(prefix="/payments", tags=["Payments"])


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
    "/initiate",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Initiate a mobile money payment",
)
async def initiate_payment(
    request: Request,
    body: PaymentInitiateRequest,
    payment_svc=Depends(get_payment_service),
):
    """
    Initiate a mobile money transfer.
    202 Accepted. Use GET /payments/{id} to poll for final status.
    """
    # Real signature: initiate(*, payer_msisdn, receiver_msisdn, amount,
    #                           currency, reference, idempotency_key, request_id, metadata)
    # Returns: tuple[PaymentTransaction | dict, bool]
    transaction, _is_duplicate = await payment_svc.initiate(
        payer_msisdn=body.payer_msisdn,
        receiver_msisdn=body.receiver_msisdn,
        amount=body.amount,
        currency=body.currency,
        reference=body.reference,
        idempotency_key=getattr(body, "idempotency_key", None),
        request_id=getattr(request.state, "request_id", None),
        metadata=getattr(body, "metadata", None),
    )
    return ApiResponse.ok(
        PaymentInitiateResponse.from_orm(transaction),
        request_id=request.state.request_id,
    )


@router.get(
    "/history",
    status_code=status.HTTP_200_OK,
    summary="List payment transactions (paginated)",
)
async def list_payments(
    request: Request,
    pagination: PaginationParams = Depends(),
    payment_svc=Depends(get_payment_service),
):
    """Return paginated payment history."""
    items, total = await payment_svc.list_history(
        skip=pagination.skip,
        limit=pagination.limit,
    )
    paginated = _build_paginated(total, pagination)
    return PaymentHistoryResponse.from_service(
        items=items,
        paginated=paginated,
        request_id=request.state.request_id,
    )


@router.get(
    "/{payment_id}",
    status_code=status.HTTP_200_OK,
    summary="Get payment status",
)
async def get_payment_status(
    request: Request,
    payment_id: str,
    payment_svc=Depends(get_payment_service),
):
    """Return the current status of a payment transaction."""
    # Real method: get_transaction(*, transaction_id: UUID)
    transaction = await payment_svc.get_transaction(
        transaction_id=UUID(payment_id)
    )
    return ApiResponse.ok(
        PaymentStatusResponse.from_orm(transaction),
        request_id=request.state.request_id,
    )