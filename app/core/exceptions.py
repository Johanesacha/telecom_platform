"""
Custom exception hierarchy for the telecom platform.
All business exceptions inherit from TelecomPlatformError.
HTTP status codes are defined here — error_handlers.py converts them to responses.
"""
from __future__ import annotations


class TelecomPlatformError(Exception):
    """Base exception. All platform errors inherit from this."""
    status_code: int = 500
    error_code: str = "GEN_001"
    message: str = "An unexpected error occurred"

    def __init__(self, message: str | None = None, **kwargs) -> None:
        self.message = message or self.__class__.message
        for key, value in kwargs.items():
            setattr(self, key, value)
        super().__init__(self.message)


# Authentication
class AuthenticationError(TelecomPlatformError):
    status_code = 401
    error_code = "AUTH_001"
    message = "Authentication required"

class InvalidApiKeyError(AuthenticationError):
    error_code = "AUTH_002"
    message = "Invalid or unknown API key"

class ExpiredApiKeyError(AuthenticationError):
    error_code = "AUTH_003"
    message = "API key has expired"

class RevokedApiKeyError(AuthenticationError):
    error_code = "AUTH_004"
    message = "API key has been revoked"

class InsufficientScopeError(TelecomPlatformError):
    status_code = 403
    error_code = "AUTH_005"
    message = "Insufficient scope for this operation"

class InsufficientRoleError(TelecomPlatformError):
    status_code = 403
    error_code = "AUTH_006"
    message = "Insufficient role for this operation"


# Rate Limiting
class RateLimitExceededError(TelecomPlatformError):
    status_code = 429
    error_code = "RATE_001"
    message = "Burst rate limit exceeded"
    retry_after: int = 60

    def __init__(self, retry_after: int = 60, **kwargs) -> None:
        self.retry_after = retry_after
        super().__init__(**kwargs)

class QuotaExceededError(TelecomPlatformError):
    status_code = 429
    error_code = "RATE_002"
    message = "Daily quota exhausted for this service"


# SMS
class InvalidMSISDNError(TelecomPlatformError):
    status_code = 422
    error_code = "SMS_001"
    message = "Invalid phone number format"

class MessageTooLongError(TelecomPlatformError):
    status_code = 422
    error_code = "SMS_002"
    message = "Message exceeds maximum length"

class DuplicateIdempotencyKeyError(TelecomPlatformError):
    status_code = 409
    error_code = "SMS_003"
    message = "A resource with this idempotency key already exists"


# USSD
class USSDSessionNotFoundError(TelecomPlatformError):
    status_code = 404
    error_code = "USSD_001"
    message = "USSD session not found"

class USSDSessionExpiredError(TelecomPlatformError):
    status_code = 410
    error_code = "USSD_002"
    message = "USSD session has expired"

class USSDInvalidInputError(TelecomPlatformError):
    status_code = 422
    error_code = "USSD_003"
    message = "Invalid menu choice"


# Payments
class InvalidAmountError(TelecomPlatformError):
    status_code = 422
    error_code = "PAY_001"
    message = "Invalid payment amount"

class DuplicatePaymentReferenceError(TelecomPlatformError):
    status_code = 409
    error_code = "PAY_002"
    message = "A transaction with this reference already exists"

class UnsupportedCurrencyError(TelecomPlatformError):
    status_code = 422
    error_code = "PAY_003"
    message = "Unsupported currency"


# General
class ResourceNotFoundError(TelecomPlatformError):
    status_code = 404
    error_code = "GEN_003"
    message = "Resource not found"

class ServiceUnavailableError(TelecomPlatformError):
    status_code = 503
    error_code = "GEN_002"
    message = "Service temporarily unavailable"