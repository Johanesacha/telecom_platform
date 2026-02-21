"""
Monetary amount utilities — Decimal safety for all payment operations.

All monetary values in this platform are Python Decimal objects.
Float is never used for money. This module provides:

  - quantize_amount(): round a Decimal to 2 decimal places
  - from_any():        safely convert float/int/str/Decimal to Decimal
  - validate_positive(): confirm amount > 0
  - validate_currency(): confirm currency is supported

Import pattern in services:
    from app.utils.money import quantize_amount, validate_positive, from_any

Why Decimal and not float:
    float("0.1") + float("0.2") == 0.30000000000000004
    Decimal("0.1") + Decimal("0.2") == Decimal("0.3")
    Financial arithmetic requires exact decimal representation.
    One float rounding error per transaction × 10,000 transactions
    = material billing discrepancy that cannot be reconciled.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from app.core.exceptions import InvalidAmountError, UnsupportedCurrencyError


# Currencies accepted by the platform.
# XOF (West African CFA franc) is the primary currency — zero decimal places
# in ISO 4217, but we store 2 decimal places for generality.
# Add currencies here only after verifying provider support.
SUPPORTED_CURRENCIES: frozenset[str] = frozenset({
    "XOF",   # CFA franc BCEAO (West Africa) — primary
    "XAF",   # CFA franc BEAC (Central Africa) — secondary
    "EUR",   # Euro — international transfers
    "USD",   # US Dollar — international transfers
})

# The standard quantisation context: 2 decimal places, ROUND_HALF_UP.
# ROUND_HALF_UP is the standard financial rounding rule:
#   0.5 rounds up to 1 (not to nearest even, which is Python's default).
# Applied consistently: 5000.505 → 5000.51, not 5000.50.
_TWO_PLACES = Decimal("0.01")


def quantize_amount(amount: Decimal) -> Decimal:
    """
    Round a Decimal to exactly 2 decimal places using ROUND_HALF_UP.

    Call this before storing any monetary amount in the database.
    The Numeric(14, 2) column type enforces 2 decimal places at the
    PostgreSQL level — passing an un-quantised Decimal risks silent
    truncation by the database driver.

    Args:
        amount: Any Decimal value.

    Returns:
        Decimal rounded to 2 places.

    Examples:
        quantize_amount(Decimal("5000.5"))   → Decimal("5000.50")
        quantize_amount(Decimal("5000.505")) → Decimal("5000.51")
        quantize_amount(Decimal("5000"))     → Decimal("5000.00")
    """
    return amount.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def from_any(value: int | float | str | Decimal) -> Decimal:
    """
    Safely convert any numeric type to Decimal.

    The conversion path matters:
      float → Decimal(float):  WRONG — inherits float's imprecision
      float → str → Decimal:   CORRECT — parses the decimal string form

    This function always routes float through str to prevent binary
    fraction contamination.

    Args:
        value: Numeric value in any acceptable type.

    Returns:
        Decimal representation.

    Raises:
        InvalidAmountError: If the value cannot be converted to Decimal.

    Examples:
        from_any(5000)        → Decimal("5000")
        from_any(5000.50)     → Decimal("5000.5")   (then quantize)
        from_any("5000.50")   → Decimal("5000.50")
        from_any(Decimal("5000.50")) → Decimal("5000.50")
    """
    if isinstance(value, Decimal):
        return value

    try:
        if isinstance(value, float):
            # Route through string to avoid binary fraction contamination.
            # repr() gives the shortest string that round-trips correctly.
            return Decimal(repr(value))
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise InvalidAmountError(
            f"Cannot convert '{value}' to Decimal: {exc}"
        ) from exc


def validate_positive(
    amount: Decimal,
    *,
    field_name: str = "amount",
) -> Decimal:
    """
    Validate that a monetary amount is strictly greater than zero.

    Zero-amount transactions are rejected — they have no business meaning
    and indicate a client error (likely a bug in the caller's integration).
    Negative amounts are also rejected — refunds use the REVERSED status
    on an existing transaction, not a negative new transaction.

    Args:
        amount:     The Decimal amount to validate.
        field_name: Field name for the error message. Defaults to 'amount'.

    Returns:
        The validated amount (unchanged).

    Raises:
        InvalidAmountError: If amount is <= 0.
    """
    if amount <= Decimal("0"):
        raise InvalidAmountError(
            f"'{field_name}' must be greater than zero. Got: {amount}"
        )
    return amount


def validate_currency(currency: str) -> str:
    """
    Validate that a currency code is supported by the platform.

    Currency codes are normalised to uppercase before checking.
    A currency not in SUPPORTED_CURRENCIES is rejected before any
    database write or provider call is made.

    Args:
        currency: ISO 4217 currency code string (e.g. "XOF", "eur").

    Returns:
        The validated currency code in uppercase.

    Raises:
        UnsupportedCurrencyError: If the currency is not in SUPPORTED_CURRENCIES.
    """
    normalised = currency.strip().upper()
    if normalised not in SUPPORTED_CURRENCIES:
        raise UnsupportedCurrencyError(
            f"Currency '{currency}' is not supported. "
            f"Accepted currencies: {sorted(SUPPORTED_CURRENCIES)}"
        )
    return normalised


def to_display_string(amount: Decimal, currency: str) -> str:
    """
    Format a Decimal amount for human-readable display.

    Used in API responses and notification messages.
    Not used for storage or arithmetic — always use Decimal for those.

    Examples:
        to_display_string(Decimal("5000.50"), "XOF") → "5 000,50 XOF"
        to_display_string(Decimal("99.99"), "EUR")   → "99,99 EUR"
    """
    quantised = quantize_amount(amount)
    # Format with space as thousands separator, comma as decimal separator
    # (West African French formatting convention)
    integer_part, decimal_part = str(quantised).split(".")
    formatted_integer = f"{int(integer_part):,}".replace(",", " ")
    return f"{formatted_integer},{decimal_part} {currency.upper()}"