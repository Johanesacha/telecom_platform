"""
API scope registry.
Scopes are stored as a list of strings on each ApiKey record.
DEFAULT_SCOPES maps plan names to their default scope list.
"""
from __future__ import annotations
from enum import StrEnum


class Scope(StrEnum):
    SMS_SEND           = "sms:send"
    SMS_READ           = "sms:read"
    USSD_MANAGE        = "ussd:manage"
    PAYMENTS_WRITE     = "payments:write"
    PAYMENTS_READ      = "payments:read"
    NUMBERS_READ       = "numbers:read"
    NOTIFICATIONS_SEND = "notifications:send"


DEFAULT_SCOPES: dict[str, list[str]] = {
    "FREE": [
        Scope.SMS_SEND,
        Scope.SMS_READ,
        Scope.NUMBERS_READ,
    ],
    "STANDARD": [
        Scope.SMS_SEND,
        Scope.SMS_READ,
        Scope.USSD_MANAGE,
        Scope.PAYMENTS_WRITE,
        Scope.PAYMENTS_READ,
        Scope.NUMBERS_READ,
        Scope.NOTIFICATIONS_SEND,
    ],
    "PREMIUM": list(Scope),  # all scopes
}