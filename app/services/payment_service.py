"""
PaymentService — dual deduplication, Decimal-safe, atomic status transitions.

Deduplication order (non-negotiable):
  1. idempotency_key check (24h cache) — return cached response immediately
  2. reference check (permanent) — raise DuplicatePaymentReferenceError
  3. Create transaction record
  4. Cache idempotency response
  5. Commit
  6. Enqueue provider Celery task

Decimal invariants:
  - All amount inputs routed through money.from_any() → validate_positive()
    → quantize_amount() before any storage or arithmetic
  - No float arithmetic anywhere in this service
  - Aggregation (sum, avg) delegated to SQL via repository methods

Status transitions:
  INITIATED → PENDING → COMPLETED | FAILED
  COMPLETED → REVERSED (admin refund only)
  Any transition from a terminal state raises InvalidAmountError.
"""
from __future__ import annotations

import secrets
from decimal import Decimal
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    DuplicatePaymentReferenceError,
    InvalidAmountError,
    ResourceNotFoundError,
    UnsupportedCurrencyError,
)
from app.domain.api_key import ApiKey, KeyType
from app.domain.payment import PaymentStatus, PaymentTransaction
from app.repositories.payment_repo import PaymentRepository
from app.services.quota_service import QuotaService
from app.utils.idempotency import cache_response, get_cached_response
from app.utils.money import (
    SUPPORTED_CURRENCIES,
    from_any,
    quantize_amount,
    validate_currency,
    validate_positive,
)
from app.utils.msisdn import parse_msisdn

_SERVICE_NAME = "payments"

# Terminal states — no further transitions allowed after reaching these
_TERMINAL_STATES: frozenset[PaymentStatus] = frozenset({
    PaymentStatus.COMPLETED,
    PaymentStatus.FAILED,
    PaymentStatus.REVERSED,
})


class PaymentService:
    """
    Orchestrates payment initiation, status management, and history.

    Instantiate per-request with session, redis, and api_key.

    Usage in route handler:
        svc = PaymentService(db, redis, api_key)
        transaction, is_duplicate = await svc.initiate(
            payer_msisdn="+221771234567",
            receiver_msisdn="+221781234567",
            amount=Decimal("5000.00"),
            currency="XOF",
            reference="ORD-2026-00847",
            idempotency_key=request.idempotency_key,
        )
        status_code = 200 if is_duplicate else 202
    """

    def __init__(
        self,
        session: AsyncSession,
        redis: aioredis.Redis,
        api_key: ApiKey,
    ) -> None:
        self._session = session
        self._redis = redis
        self._api_key = api_key
        self._app_id: UUID = api_key.application_id
        self._is_sandbox: bool = api_key.key_type == KeyType.SANDBOX
        self._repo = PaymentRepository(session)
        self._quota = QuotaService(api_key, redis)

    # ── Payment Initiation ─────────────────────────────────────────────────

    async def initiate(
        self,
        *,
        payer_msisdn: str,
        receiver_msisdn: str,
        amount: Decimal | float | str | int,
        currency: str,
        reference: str,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        metadata: dict | None = None,
    ) -> tuple[PaymentTransaction | dict, bool]:
        """
        Initiate a payment transaction.

        Enforces dual deduplication:
          1. idempotency_key → return cached response if found (HTTP 200)
          2. reference → raise DuplicatePaymentReferenceError if found (HTTP 409)

        Returns:
            (transaction_or_cached, is_duplicate)
        """
        # ── Step 1: Idempotency check FIRST ───────────────────────────────
        if idempotency_key is not None:
            cached = await get_cached_response(
                self._redis, self._app_id, _SERVICE_NAME, idempotency_key
            )
            if cached is not None:
                return cached, True

        # ── Step 2: Quota check ────────────────────────────────────────────
        await self._quota.check_and_consume(_SERVICE_NAME)

        # ── Step 3: Validate inputs ────────────────────────────────────────
        validated_amount = _validate_and_prepare_amount(amount)
        validated_currency = validate_currency(currency)

        payer_info = parse_msisdn(payer_msisdn)
        receiver_info = parse_msisdn(receiver_msisdn)
        operator = payer_info.operator

        # ── Step 4: Reference deduplication check ─────────────────────────
        existing = await self._repo.get_by_reference(self._app_id, reference)
        if existing is not None:
            raise DuplicatePaymentReferenceError(
                f"A transaction with reference '{reference}' already exists "
                f"for this application. "
                f"Existing transaction ID: {existing.id}, "
                f"status: {existing.status}."
            )

        # ── Step 5: Database write with race condition handling ────────────
        nonce = secrets.token_hex(32)

        transaction, is_duplicate = await self._create_with_race_protection(
            payer_msisdn=payer_info.e164,
            receiver_msisdn=receiver_info.e164,
            amount=validated_amount,
            currency=validated_currency,
            reference=reference,
            nonce=nonce,
            idempotency_key=idempotency_key,
            request_id=request_id,
            operator=operator,
            metadata=metadata or {},
        )

        if is_duplicate:
            await self._quota_rollback()
            return transaction, True

        # ── Step 6: Cache idempotency response ────────────────────────────
        if idempotency_key is not None:
            await cache_response(
                self._redis, self._app_id, _SERVICE_NAME,
                idempotency_key,
                _serialise_transaction(transaction),
            )

        # ── Step 7: Commit ─────────────────────────────────────────────────
        await self._session.commit()

        # ── Step 8: Sandbox resolution or Celery enqueue ───────────────────
        if self._is_sandbox:
            sandbox_status = _sandbox_status_for(payer_info.e164)
            await self._repo.update_status(transaction, sandbox_status)
            await self._session.commit()

        return transaction, False

    # ── Status Management ──────────────────────────────────────────────────

    async def get_transaction(self, *, transaction_id: UUID) -> PaymentTransaction:
        """
        Fetch a single transaction owned by this application.

        Raises:
            ResourceNotFoundError: Not found or belongs to another application.
        """
        tx = await self._repo.get_by_id_for_application(
            transaction_id, self._app_id
        )
        if tx is None:
            raise ResourceNotFoundError(
                f"Payment transaction {transaction_id} not found"
            )
        return tx

    async def mark_pending(self, *, transaction_id: UUID) -> PaymentTransaction:
        """Transition INITIATED → PENDING after provider acknowledges."""
        tx = await self._get_and_validate_transition(
            transaction_id,
            expected_current=PaymentStatus.INITIATED,
            new_status=PaymentStatus.PENDING,
        )
        updated = await self._repo.update_status(tx, PaymentStatus.PENDING)
        await self._session.commit()
        return updated

    async def mark_completed(self, *, transaction_id: UUID) -> PaymentTransaction:
        """Transition PENDING → COMPLETED after provider confirms success."""
        tx = await self._get_and_validate_transition(
            transaction_id,
            expected_current=PaymentStatus.PENDING,
            new_status=PaymentStatus.COMPLETED,
        )
        updated = await self._repo.update_status(tx, PaymentStatus.COMPLETED)
        await self._session.commit()
        return updated

    async def mark_failed(
        self,
        *,
        transaction_id: UUID,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> PaymentTransaction:
        """Transition PENDING → FAILED after provider rejects. Rolls back quota."""
        tx = await self._get_and_validate_transition(
            transaction_id,
            expected_current=PaymentStatus.PENDING,
            new_status=PaymentStatus.FAILED,
        )
        updated = await self._repo.update_status(tx, PaymentStatus.FAILED)
        await self._session.commit()
        await self._quota_rollback()
        return updated

    async def reverse_transaction(self, *, transaction_id: UUID) -> PaymentTransaction:
        """Transition COMPLETED → REVERSED (admin-initiated refund)."""
        tx = await self._get_and_validate_transition(
            transaction_id,
            expected_current=PaymentStatus.COMPLETED,
            new_status=PaymentStatus.REVERSED,
        )
        updated = await self._repo.update_status(tx, PaymentStatus.REVERSED)
        await self._session.commit()
        return updated

    # ── History & Analytics ────────────────────────────────────────────────

    async def list_history(
        self,
        *,
        skip: int = 0,
        limit: int = 20,
        status_filter: PaymentStatus | None = None,
    ) -> tuple[list[PaymentTransaction], int]:
        """Return paginated transaction history. Returns (items, total)."""
        total = await self._repo.count_by_application(
            self._app_id, status_filter=status_filter
        )
        items = await self._repo.list_by_application(
            self._app_id,
            skip=skip,
            limit=limit,
            status_filter=status_filter,
        )
        return items, total

    async def get_usage_summary(self) -> dict:
        """
        Return Decimal-safe payment analytics for this application.

        All amounts returned as strings to prevent JSON float coercion.
        """
        total_volume = await self._repo.sum_completed_amount(self._app_id)
        by_status = await self._repo.count_by_status(self._app_id)
        by_operator = await self._repo.average_amount_by_operator(self._app_id)

        return {
            "total_completed_volume": str(quantize_amount(total_volume)),
            "transaction_counts": by_status,
            "average_by_operator": {
                op: str(quantize_amount(avg))
                for op, avg in by_operator.items()
            },
        }

    # ── Private Helpers ────────────────────────────────────────────────────

    async def _create_with_race_protection(
        self,
        *,
        payer_msisdn: str,
        receiver_msisdn: str,
        amount: Decimal,
        currency: str,
        reference: str,
        nonce: str,
        idempotency_key: str | None,
        request_id: str | None,
        operator: str,
        metadata: dict,
    ) -> tuple[PaymentTransaction, bool]:
        """Create a PaymentTransaction with IntegrityError race handling."""
        try:
            tx = await self._repo.create(
                application_id=self._app_id,
                payer_msisdn=payer_msisdn,
                receiver_msisdn=receiver_msisdn,
                amount=amount,
                currency=currency,
                reference=reference,
                idempotency_key=idempotency_key,
                status=PaymentStatus.INITIATED,
                nonce=nonce,
                operator=operator,
                request_id=request_id,
                metadata_=metadata,
                is_sandbox=self._is_sandbox,
            )
            return tx, False

        except IntegrityError:
            await self._session.rollback()

            if idempotency_key is None:
                raise

            existing = await self._repo.get_by_idempotency_key(
                self._app_id, idempotency_key
            )
            if existing is None:
                raise

            return existing, True

    async def _get_and_validate_transition(
        self,
        transaction_id: UUID,
        *,
        expected_current: PaymentStatus,
        new_status: PaymentStatus,
    ) -> PaymentTransaction:
        """Fetch and validate that a status transition is legal."""
        tx = await self._repo.get_by_id_for_application(
            transaction_id, self._app_id
        )
        if tx is None:
            raise ResourceNotFoundError(
                f"Payment transaction {transaction_id} not found"
            )

        if tx.status in _TERMINAL_STATES:
            raise InvalidAmountError(
                f"Transaction {transaction_id} is in terminal state "
                f"'{tx.status}' and cannot be transitioned to '{new_status}'."
            )

        if tx.status != expected_current:
            raise InvalidAmountError(
                f"Cannot transition from '{tx.status}' to '{new_status}'. "
                f"Expected current status: '{expected_current}'."
            )

        return tx

    async def _quota_rollback(self) -> None:
        """Decrement the payments daily quota by one on permanent failure."""
        from app.utils.time_utils import today_utc_str
        key = f"quota:{self._app_id}:{_SERVICE_NAME}:{today_utc_str()}"
        await self._redis.decr(key)


# ── Module-level pure functions ────────────────────────────────────────────

def _validate_and_prepare_amount(raw: Decimal | float | str | int) -> Decimal:
    """
    Full Decimal preparation pipeline for any incoming amount value.

    Pipeline: from_any() → validate_positive() → quantize_amount()
    """
    amount = from_any(raw)
    validate_positive(amount)
    return quantize_amount(amount)


def _sandbox_status_for(payer_e164: str) -> PaymentStatus:
    """
    Deterministic sandbox outcome based on last digit of payer MSISDN.

    Last digit 9 → FAILED
    All others  → COMPLETED
    """
    last_digit = next(
        (ch for ch in reversed(payer_e164) if ch.isdigit()),
        "1",
    )
    return (
        PaymentStatus.FAILED
        if last_digit == "9"
        else PaymentStatus.COMPLETED
    )


def _serialise_transaction(tx: PaymentTransaction) -> dict:
    """Convert a PaymentTransaction to a JSON-safe dict for idempotency cache."""
    return {
        "id": str(tx.id),
        "reference": tx.reference,
        "status": tx.status,
        "amount": str(tx.amount),
        "currency": tx.currency,
        "payer_msisdn": tx.payer_msisdn,
        "is_sandbox": tx.is_sandbox,
        "created_at": tx.created_at.isoformat() if tx.created_at else None,
    }