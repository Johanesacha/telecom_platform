"""
Abstract base classes for all external service providers.

Provider isolation contract:
  - Providers import NOTHING from app.services, app.schemas, app.repositories
  - Providers receive only primitive Python types (str, Decimal, dict)
  - Providers return only ProviderResult dataclasses
  - All provider methods are async

This isolation enables:
  1. Provider unit tests without database or Redis
  2. Real provider swap (Orange SMSC → base class) without service changes
  3. Deterministic sandbox provider injection in test suite

ProviderResult is a dataclass (not Pydantic):
  Internal value object — never crosses an API boundary.
  Pydantic overhead is unnecessary for internal return values.
  dataclass gives __repr__, __eq__, and type hints at zero runtime cost.

Usage in services:
  provider = MockSMSProvider()              # or SandboxSMSProvider()
  result = await provider.send(
      to="+221771234567",
      message="Your OTP is 123456",
      from_alias="TELECOM",
  )
  if result.success:
      sms_repo.update_status(msg, SMSStatus.SENT,
                             provider_message_id=result.provider_message_id)
  else:
      sms_repo.update_status(msg, SMSStatus.FAILED,
                             error_message=result.error_message)
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from decimal import Decimal


# ── Result Value Object ────────────────────────────────────────────────────

@dataclass
class ProviderResult:
    """
    Return value from every provider method.

    success:             True if the provider accepted the request.
    provider_message_id: ID assigned by the provider for tracking.
                         Present on success, None on failure.
    error_message:       Human-readable failure description.
                         Present on failure, None on success.
                         Truncated to 500 chars before DB storage.
    raw_response:        The complete provider response payload.
                         Used for audit log debugging — not parsed by services.
                         May contain provider-specific status codes, timestamps,
                         and routing information useful when debugging delivery
                         failures with the provider's support team.
    """
    success: bool
    provider_message_id: str | None = field(default=None)
    error_message: str | None = field(default=None)
    raw_response: dict | None = field(default=None)

    def __post_init__(self) -> None:
        """
        Enforce internal consistency:
          success=True  requires provider_message_id to be present
          success=False requires error_message to be present

        Raises ValueError on construction if the invariant is violated.
        This catches programming errors (wrong success/error combination)
        at the point of construction, not silently downstream.
        """
        if self.success and self.provider_message_id is None:
            raise ValueError(
                "ProviderResult with success=True must have provider_message_id. "
                "Provide a non-None provider_message_id or set success=False."
            )
        if not self.success and self.error_message is None:
            raise ValueError(
                "ProviderResult with success=False must have error_message. "
                "Provide a non-None error_message or set success=True."
            )

    @classmethod
    def ok(
        cls,
        provider_message_id: str,
        raw_response: dict | None = None,
    ) -> "ProviderResult":
        """Convenience constructor for successful results."""
        return cls(
            success=True,
            provider_message_id=provider_message_id,
            raw_response=raw_response,
        )

    @classmethod
    def fail(
        cls,
        error_message: str,
        raw_response: dict | None = None,
    ) -> "ProviderResult":
        """Convenience constructor for failed results."""
        return cls(
            success=False,
            error_message=error_message,
            raw_response=raw_response,
        )


# ── Abstract Base Classes ──────────────────────────────────────────────────

class BaseSMSProvider(abc.ABC):
    """
    Abstract contract for SMS delivery providers.

    Concrete implementations:
      MockSMSProvider    — realistic simulation with configurable failure rate
      SandboxSMSProvider — deterministic by MSISDN last digit, zero delay

    Production implementation (not in scope):
      OrangeSMSProvider  — Orange Sénégal SMSC HTTP API
      FreeSMSProvider    — Free Sénégal SMSC HTTP API
    """

    @abc.abstractmethod
    async def send(
        self,
        *,
        to: str,
        message: str,
        from_alias: str | None = None,
    ) -> ProviderResult:
        """
        Submit an SMS for delivery.

        Args:
            to:         Recipient E.164 phone number.
            message:    SMS body text. Provider may reject if too long.
            from_alias: Sender ID (e.g. 'TELECOM'). None uses provider default.

        Returns:
            ProviderResult with success=True and provider_message_id on acceptance.
            ProviderResult with success=False and error_message on rejection.

        Note: success=True means the provider ACCEPTED the message for delivery.
        It does not guarantee delivery to the handset — that is a separate event
        (delivery receipt) handled by the Celery task's webhook handler.
        """

    @abc.abstractmethod
    async def check_delivery(
        self,
        *,
        provider_message_id: str,
    ) -> ProviderResult:
        """
        Poll delivery status for a previously submitted message.

        Called by the Celery task when the provider does not push
        delivery receipts and the task must poll instead.

        Args:
            provider_message_id: The ID returned by send() on success.

        Returns:
            ProviderResult reflecting current delivery state.
            success=True means delivered to handset.
            success=False means delivery failed or is still pending.
        """


class BasePaymentProvider(abc.ABC):
    """
    Abstract contract for mobile money payment providers.

    Mobile money in Senegal operates via USSD-based APIs specific to each
    operator. Orange Money uses a different API from Wave, Free Money,
    and Expresso. The platform abstracts this behind a single interface.
    """

    @abc.abstractmethod
    async def initiate(
        self,
        *,
        payer_msisdn: str,
        receiver_msisdn: str,
        amount: Decimal,
        currency: str,
        reference: str,
    ) -> ProviderResult:
        """
        Initiate a mobile money transfer.

        Args:
            payer_msisdn:    E.164 MSISDN of the payer.
            receiver_msisdn: E.164 MSISDN of the receiver.
            amount:          Decimal amount — provider receives Decimal, not float.
            currency:        ISO 4217 currency code (e.g. 'XOF').
            reference:       Client reference for deduplication at provider level.

        Returns:
            ProviderResult with provider_message_id on initiation acceptance.
            The payment is PENDING until confirmed via check_status().
        """

    @abc.abstractmethod
    async def check_status(
        self,
        *,
        provider_message_id: str,
    ) -> ProviderResult:
        """
        Poll payment status from the provider.

        Called by the Celery payment task when polling is required.
        success=True in this context means the payment COMPLETED,
        not merely that the poll request succeeded.

        Args:
            provider_message_id: The ID returned by initiate() on success.
        """


class BaseNotificationProvider(abc.ABC):
    """
    Abstract contract for notification delivery providers.

    Unifies SMS (short message), EMAIL (SMTP/API), and PUSH (FCM/APNs)
    behind one interface for the NotificationService channel router.
    """

    @abc.abstractmethod
    async def send(
        self,
        *,
        channel: str,
        recipient: str,
        body: str,
        subject: str | None = None,
    ) -> ProviderResult:
        """
        Dispatch a notification through the specified channel.

        Args:
            channel:   'SMS', 'EMAIL', or 'PUSH'.
            recipient: Channel-appropriate address (E.164 / email / device token).
            body:      Notification content.
            subject:   Subject line (EMAIL only — ignored for SMS and PUSH).

        Returns:
            ProviderResult with provider_message_id on acceptance.
        """