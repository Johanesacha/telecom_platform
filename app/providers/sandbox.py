"""
Sandbox providers — deterministic, zero-delay, no randomness.

Determinism contract (documented in API reference):

  SMS provider:
    Last digit of recipient E.164 number:
      8 → FAILED   (use +221771234568 to test failure path)
      * → DELIVERED (any other last digit)

  Payment provider:
    Last digit of payer E.164 number:
      9 → FAILED   (use +221771234569 to test failure path)
      * → COMPLETED (any other last digit)

  Notification provider:
    Always SENT — sandbox notifications always succeed.
    Rationale: notification failure is a provider concern,
    not a platform routing concern. The routing logic is what
    developers need to test, not provider acceptance behaviour.

Zero delay contract:
  No asyncio.sleep() anywhere in this file.
  Sandbox providers return immediately — test suites run in milliseconds.

No randomness contract:
  random module is not imported. The outcome of any call is a pure
  function of the input MSISDN digits. Given the same inputs, the
  same result is returned every time without exception.
"""
from __future__ import annotations

import secrets
from decimal import Decimal

from app.providers.base import (
    BaseNotificationProvider,
    BasePaymentProvider,
    BaseSMSProvider,
    ProviderResult,
)


def _last_digit(msisdn: str) -> str:
    """
    Extract the last digit from an E.164 MSISDN string.

    Scans right-to-left for the first digit character.
    Returns '0' as a safe default if no digit is found
    (should not occur for valid E.164 numbers).
    """
    return next(
        (ch for ch in reversed(msisdn) if ch.isdigit()),
        "0",
    )


def _fake_sandbox_id(prefix: str) -> str:
    """
    Generate a determinism-preserving sandbox provider message ID.

    Format: sandbox-{prefix}-{8 hex chars}
    The hex suffix is random but this is acceptable — the ID is not
    used for outcome determination (last digit rule controls outcome).
    The ID just needs to be unique per call and recognisable as sandbox.
    """
    return f"sandbox-{prefix}-{secrets.token_hex(4)}"


# ── Sandbox SMS Provider ───────────────────────────────────────────────────

class SandboxSMSProvider(BaseSMSProvider):
    """
    Deterministic SMS provider for sandbox API key calls.

    Outcome rule:
      Recipient last digit == '8' → FAILED
      All other last digits       → DELIVERED

    Test numbers:
      +221771234568 → always fails  (test quota rollback, FAILED status)
      +221771234567 → always passes (test happy path, DELIVERED status)
    """

    async def send(
        self,
        *,
        to: str,
        message: str,
        from_alias: str | None = None,
    ) -> ProviderResult:
        last = _last_digit(to)

        if last == "8":
            return ProviderResult.fail(
                error_message=(
                    f"SANDBOX_FAILURE: Recipient '{to}' ends in digit 8 — "
                    "configured to simulate delivery failure in sandbox mode. "
                    "Use a number ending in any other digit for successful delivery."
                ),
                raw_response={
                    "status": "SANDBOX_FAILED",
                    "destination": to,
                    "rule": "last_digit_8_always_fails",
                },
            )

        msg_id = _fake_sandbox_id("sms")
        return ProviderResult.ok(
            provider_message_id=msg_id,
            raw_response={
                "status": "SANDBOX_DELIVERED",
                "message_id": msg_id,
                "destination": to,
                "rule": "last_digit_not_8_always_delivered",
            },
        )

    async def check_delivery(
        self,
        *,
        provider_message_id: str,
    ) -> ProviderResult:
        """
        Sandbox delivery check — always reports DELIVERED.

        The outcome was determined at send() time. If send() succeeded,
        the message is considered delivered immediately in sandbox mode.
        Celery tasks polling sandbox messages receive DELIVERED at first poll.
        """
        return ProviderResult.ok(
            provider_message_id=provider_message_id,
            raw_response={
                "status": "SANDBOX_DELIVERED",
                "message_id": provider_message_id,
                "rule": "sandbox_check_always_delivered",
            },
        )


# ── Sandbox Payment Provider ───────────────────────────────────────────────

class SandboxPaymentProvider(BasePaymentProvider):
    """
    Deterministic payment provider for sandbox API key calls.

    Outcome rule:
      Payer last digit == '9' → FAILED
      All other last digits   → COMPLETED

    Test MSISDNs:
      +221771234569 → always fails  (test insufficient funds, rollback)
      +221771234567 → always passes (test completed payment)

    The distinct rule (digit 9 not digit 8) allows developers to
    use different numbers for SMS failure vs payment failure tests
    without accidentally triggering both failure conditions at once.
    """

    async def initiate(
        self,
        *,
        payer_msisdn: str,
        receiver_msisdn: str,
        amount: Decimal,
        currency: str,
        reference: str,
    ) -> ProviderResult:
        last = _last_digit(payer_msisdn)

        if last == "9":
            return ProviderResult.fail(
                error_message=(
                    f"SANDBOX_FAILURE: Payer '{payer_msisdn}' ends in digit 9 — "
                    "configured to simulate payment failure in sandbox mode. "
                    "Use a payer number ending in any other digit for successful payment."
                ),
                raw_response={
                    "status": "SANDBOX_FAILED",
                    "payer": payer_msisdn,
                    "reference": reference,
                    "amount": str(amount),
                    "currency": currency,
                    "rule": "last_digit_9_always_fails",
                },
            )

        txn_id = _fake_sandbox_id("pay")
        return ProviderResult.ok(
            provider_message_id=txn_id,
            raw_response={
                "status": "SANDBOX_COMPLETED",
                "transaction_id": txn_id,
                "payer": payer_msisdn,
                "receiver": receiver_msisdn,
                "amount": str(amount),
                "currency": currency,
                "reference": reference,
                "rule": "last_digit_not_9_always_completed",
            },
        )

    async def check_status(
        self,
        *,
        provider_message_id: str,
    ) -> ProviderResult:
        """
        Sandbox status check — always reports COMPLETED.

        Same reasoning as SandboxSMSProvider.check_delivery().
        """
        return ProviderResult.ok(
            provider_message_id=provider_message_id,
            raw_response={
                "status": "SANDBOX_COMPLETED",
                "transaction_id": provider_message_id,
                "rule": "sandbox_check_always_completed",
            },
        )


# ── Sandbox Notification Provider ──────────────────────────────────────────

class SandboxNotificationProvider(BaseNotificationProvider):
    """
    Always-succeeding notification provider for sandbox API key calls.

    All channels (SMS, EMAIL, PUSH) always return success immediately.

    Rationale:
      Notification delivery success/failure depends on external provider
      state (mailbox full, device token expired) — not on the platform's
      routing logic. Integration tests for notifications should verify
      that the platform routes correctly to the right channel and builds
      the correct recipient address, not that the downstream provider
      accepts the message.

      Always-succeed sandbox behaviour keeps tests focused on the correct
      concern (routing) rather than the incorrect concern (delivery outcome).
    """

    async def send(
        self,
        *,
        channel: str,
        recipient: str,
        body: str,
        subject: str | None = None,
    ) -> ProviderResult:
        msg_id = _fake_sandbox_id(channel.lower()[:4])
        response: dict = {
            "status": "SANDBOX_SENT",
            "channel": channel,
            "message_id": msg_id,
            "recipient": recipient,
            "rule": "sandbox_notifications_always_succeed",
        }
        if subject:
            response["subject"] = subject

        return ProviderResult.ok(
            provider_message_id=msg_id,
            raw_response=response,
        )