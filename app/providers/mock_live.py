"""
Mock live providers — realistic simulation for development and staging.

Behaviour:
  - Configurable failure_rate (default 5%) — random.random() < failure_rate → fail
  - Async delay: asyncio.sleep(uniform(0.1, 0.3)) — simulates real SMSC round-trip
  - Realistic error messages drawn from common provider error patterns
  - Fake but format-correct provider_message_ids

Use in service constructors:
  provider = MockSMSProvider(failure_rate=0.05)  # 5% failure rate (default)
  provider = MockSMSProvider(failure_rate=1.0)   # always fail (rollback testing)
  provider = MockSMSProvider(failure_rate=0.0)   # never fail (happy path testing)

No imports from app.services, app.schemas, or app.repositories.
"""
from __future__ import annotations

import random
import secrets
import asyncio
from decimal import Decimal

from app.providers.base import BaseNotificationProvider, BasePaymentProvider, BaseSMSProvider, ProviderResult

# Realistic provider error messages sampled from real SMSC error catalogues
_SMS_ERRORS = [
    "MSISDN_UNREACHABLE: The destination number is currently unreachable",
    "INSUFFICIENT_CREDIT: Subscriber has insufficient balance for premium SMS",
    "SPAM_FILTER_REJECT: Message body matched spam filter rule 0x2A",
    "ROUTE_NOT_FOUND: No valid route found for destination network prefix",
    "THROUGHPUT_EXCEEDED: Message rate limit exceeded for this originator",
]

_PAYMENT_ERRORS = [
    "PAYER_INSUFFICIENT_FUNDS: Payer mobile money account balance is insufficient",
    "PAYER_ACCOUNT_FROZEN: Payer account is temporarily suspended",
    "DAILY_LIMIT_EXCEEDED: Transaction would exceed payer's daily transfer limit",
    "RECEIVER_KYC_INCOMPLETE: Receiver account KYC verification is incomplete",
    "OPERATOR_TIMEOUT: Mobile money operator did not respond within 30 seconds",
    "TRANSACTION_DECLINED: Transaction declined by operator risk engine",
]

_EMAIL_ERRORS = [
    "RECIPIENT_MAILBOX_FULL: Recipient mailbox is over quota",
    "DOMAIN_NOT_FOUND: Recipient domain MX record not found",
    "SPAM_SCORE_EXCEEDED: Message spam score (8.2) exceeds threshold (5.0)",
    "RELAY_REJECTED: Relay access denied by recipient mail server",
]

_PUSH_ERRORS = [
    "INVALID_TOKEN: Device registration token is expired or malformed",
    "APP_UNINSTALLED: Target application has been uninstalled from device",
    "QUOTA_EXCEEDED: Firebase project message quota exceeded for today",
]

# Simulated provider response latency range (seconds)
_MIN_DELAY: float = 0.1
_MAX_DELAY: float = 0.3


def _fake_sms_id() -> str:
    """Generate a plausible SMSC message ID."""
    return f"MSG{secrets.token_hex(6).upper()}"


def _fake_payment_id() -> str:
    """Generate a plausible mobile money transaction ID."""
    return f"TXN-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}"


def _fake_email_id() -> str:
    """Generate a plausible SMTP message ID."""
    return f"<{secrets.token_hex(8)}@mail.telecomplatform.sn>"


def _fake_push_id() -> str:
    """Generate a plausible FCM message ID."""
    return f"projects/telecom-platform/messages/{secrets.token_hex(10)}"


async def _simulate_network_delay() -> None:
    """
    Simulate real provider network round-trip latency.

    asyncio.sleep is non-blocking — the event loop handles other requests
    during this await. This is the correct way to simulate I/O latency
    without blocking the thread.
    """
    delay = random.uniform(_MIN_DELAY, _MAX_DELAY)
    await asyncio.sleep(delay)


def _should_fail(failure_rate: float) -> bool:
    """Return True if this request should simulate a provider failure."""
    return random.random() < failure_rate


# ── Mock SMS Provider ──────────────────────────────────────────────────────

class MockSMSProvider(BaseSMSProvider):
    """
    Realistic SMS provider simulation for development and staging.

    Args:
        failure_rate: Probability of simulated failure per request (0.0–1.0).
                      Default 0.05 (5%) matches real-world SMSC failure rates.
    """

    def __init__(self, failure_rate: float = 0.05) -> None:
        if not 0.0 <= failure_rate <= 1.0:
            raise ValueError(
                f"failure_rate must be between 0.0 and 1.0. Got: {failure_rate}"
            )
        self.failure_rate = failure_rate

    async def send(
        self,
        *,
        to: str,
        message: str,
        from_alias: str | None = None,
    ) -> ProviderResult:
        """Submit SMS with simulated latency and configurable failure rate."""
        await _simulate_network_delay()

        if _should_fail(self.failure_rate):
            error = random.choice(_SMS_ERRORS)
            return ProviderResult.fail(
                error_message=error,
                raw_response={
                    "status": "ERROR",
                    "error_code": error.split(":")[0],
                    "destination": to,
                    "timestamp": _now_iso(),
                },
            )

        msg_id = _fake_sms_id()
        return ProviderResult.ok(
            provider_message_id=msg_id,
            raw_response={
                "status": "ACCEPTED",
                "message_id": msg_id,
                "destination": to,
                "segments": len(message) // 160 + 1,
                "timestamp": _now_iso(),
            },
        )

    async def check_delivery(
        self,
        *,
        provider_message_id: str,
    ) -> ProviderResult:
        """
        Poll delivery status — mock always reports DELIVERED after initial delay.

        In production this would query the SMSC API with the message ID.
        """
        await _simulate_network_delay()

        if _should_fail(self.failure_rate * 0.5):
            # Delivery failures are less common than submission failures
            return ProviderResult.fail(
                error_message="DELIVERY_FAILED: Message expired before delivery",
                raw_response={
                    "status": "UNDELIVERABLE",
                    "message_id": provider_message_id,
                    "timestamp": _now_iso(),
                },
            )

        return ProviderResult.ok(
            provider_message_id=provider_message_id,
            raw_response={
                "status": "DELIVERED",
                "message_id": provider_message_id,
                "delivered_at": _now_iso(),
            },
        )


# ── Mock Payment Provider ──────────────────────────────────────────────────

class MockPaymentProvider(BasePaymentProvider):
    """
    Realistic mobile money provider simulation.

    Args:
        failure_rate: Probability of simulated failure (default 0.05).
    """

    def __init__(self, failure_rate: float = 0.05) -> None:
        if not 0.0 <= failure_rate <= 1.0:
            raise ValueError(
                f"failure_rate must be between 0.0 and 1.0. Got: {failure_rate}"
            )
        self.failure_rate = failure_rate

    async def initiate(
        self,
        *,
        payer_msisdn: str,
        receiver_msisdn: str,
        amount: Decimal,
        currency: str,
        reference: str,
    ) -> ProviderResult:
        """Initiate payment with simulated latency and failure rate."""
        await _simulate_network_delay()

        if _should_fail(self.failure_rate):
            error = random.choice(_PAYMENT_ERRORS)
            return ProviderResult.fail(
                error_message=error,
                raw_response={
                    "status": "FAILED",
                    "error_code": error.split(":")[0],
                    "reference": reference,
                    "amount": str(amount),
                    "currency": currency,
                    "timestamp": _now_iso(),
                },
            )

        txn_id = _fake_payment_id()
        return ProviderResult.ok(
            provider_message_id=txn_id,
            raw_response={
                "status": "PENDING",
                "transaction_id": txn_id,
                "reference": reference,
                "amount": str(amount),
                "currency": currency,
                "payer": payer_msisdn,
                "receiver": receiver_msisdn,
                "timestamp": _now_iso(),
            },
        )

    async def check_status(
        self,
        *,
        provider_message_id: str,
    ) -> ProviderResult:
        """Poll payment completion status."""
        await _simulate_network_delay()

        if _should_fail(self.failure_rate):
            return ProviderResult.fail(
                error_message="TRANSACTION_DECLINED: Payment declined by operator",
                raw_response={
                    "status": "FAILED",
                    "transaction_id": provider_message_id,
                    "timestamp": _now_iso(),
                },
            )

        return ProviderResult.ok(
            provider_message_id=provider_message_id,
            raw_response={
                "status": "COMPLETED",
                "transaction_id": provider_message_id,
                "completed_at": _now_iso(),
            },
        )


# ── Mock Notification Provider ─────────────────────────────────────────────

class MockNotificationProvider(BaseNotificationProvider):
    """
    Realistic notification provider simulation covering SMS, EMAIL, and PUSH.

    Uses channel-specific error pools to simulate realistic failure modes.

    Args:
        failure_rate: Probability of simulated failure (default 0.05).
    """

    def __init__(self, failure_rate: float = 0.05) -> None:
        if not 0.0 <= failure_rate <= 1.0:
            raise ValueError(
                f"failure_rate must be between 0.0 and 1.0. Got: {failure_rate}"
            )
        self.failure_rate = failure_rate

    # Channel-to-error-pool and ID-generator mapping
    _ERROR_POOLS: dict[str, list[str]] = {
        "SMS": _SMS_ERRORS,
        "EMAIL": _EMAIL_ERRORS,
        "PUSH": _PUSH_ERRORS,
    }
    _ID_GENERATORS = {
        "SMS": _fake_sms_id,
        "EMAIL": _fake_email_id,
        "PUSH": _fake_push_id,
    }

    async def send(
        self,
        *,
        channel: str,
        recipient: str,
        body: str,
        subject: str | None = None,
    ) -> ProviderResult:
        """Dispatch notification with simulated latency and channel-specific errors."""
        await _simulate_network_delay()

        error_pool = self._ERROR_POOLS.get(channel.upper(), _SMS_ERRORS)
        id_gen = self._ID_GENERATORS.get(channel.upper(), _fake_sms_id)

        if _should_fail(self.failure_rate):
            error = random.choice(error_pool)
            return ProviderResult.fail(
                error_message=error,
                raw_response={
                    "status": "FAILED",
                    "channel": channel,
                    "recipient": recipient,
                    "error_code": error.split(":")[0],
                    "timestamp": _now_iso(),
                },
            )

        msg_id = id_gen()
        response: dict = {
            "status": "ACCEPTED",
            "channel": channel,
            "message_id": msg_id,
            "recipient": recipient,
            "timestamp": _now_iso(),
        }
        if subject:
            response["subject"] = subject

        return ProviderResult.ok(
            provider_message_id=msg_id,
            raw_response=response,
        )


# ── Internal helper ────────────────────────────────────────────────────────

def _now_iso() -> str:
    """Return current UTC timestamp as ISO 8601 string."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()