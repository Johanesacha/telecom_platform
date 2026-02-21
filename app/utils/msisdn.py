"""
MSISDN (phone number) normalisation and operator detection.

All phone numbers entering the platform are normalised to E.164 format
before storage or transmission. Operator detection uses a static prefix
table — accurate for Senegal as of 2026.

The country_hint parameter defaults to 'SN' (Senegal) because this
platform primarily serves the Senegalese market. Pass a different
ISO 3166-1 alpha-2 code for international numbers.
"""
from __future__ import annotations

from dataclasses import dataclass

import phonenumbers
from phonenumbers import NumberParseException

from app.core.exceptions import InvalidMSISDNError


# ── Senegal Operator Prefix Table ──────────────────────────────────────────
# Source: ARTP (Autorité de Régulation des Télécommunications et des Postes)
# Mobile prefixes are the first two digits of the 9-digit subscriber number.
# Updated: February 2026.
#
# Only prefixes accepted as valid by the phonenumbers library are listed.
# 75 (Free) and 33 (Expresso) are rejected by libphonenumber for SN —
# numbers with those prefixes will parse successfully but return UNKNOWN
# as operator until the library's metadata is updated.
#
# Orange Sénégal (Sonatel) : 77, 78, 70
# Free Sénégal             : 76
# Expresso Sénégal         : (33 not recognised by phonenumbers — UNKNOWN)

_SN_MOBILE_PREFIXES: dict[str, str] = {
    "77": "ORANGE",
    "78": "ORANGE",
    "70": "ORANGE",
    "76": "FREE",
}


@dataclass(frozen=True)
class MSISDNInfo:
    """
    Result of a successful MSISDN parse and classification.

    All fields are populated after a successful parse.
    Never instantiated directly — returned by parse_msisdn().
    """
    e164: str             # E.164 format: +221771234567
    national: str         # National format: 77 123 45 67
    country_code: str     # Dial prefix: +221
    country_iso: str      # ISO 3166-1 alpha-2: SN
    operator: str         # ORANGE / FREE / EXPRESSO / UNKNOWN
    is_mobile: bool       # True for mobile lines
    is_valid: bool        # Always True — invalid numbers raise, not return False


def parse_msisdn(
    raw: str,
    *,
    country_hint: str = "SN",
) -> MSISDNInfo:
    """
    Parse, validate, and classify a phone number string.

    Accepts any reasonable phone number format:
      - Local:         77 123 45 67
      - National:      0771234567  (not standard in SN but accepted)
      - International: +221 77 123 45 67
      - Compact:       221771234567
      - With dashes:   77-123-45-67

    The phonenumbers library handles format variations.
    This function handles operator classification.

    Args:
        raw:          Raw phone number string from the API request.
        country_hint: ISO 3166-1 alpha-2 country code for local numbers.
                      Defaults to 'SN' (Senegal).

    Returns:
        MSISDNInfo dataclass with all classification fields populated.

    Raises:
        InvalidMSISDNError: If the number cannot be parsed or is not
                            a valid number in the given country context.
    """
    raw = raw.strip()
    if not raw:
        raise InvalidMSISDNError("Phone number cannot be empty")

    try:
        parsed = phonenumbers.parse(raw, country_hint)
    except NumberParseException as exc:
        raise InvalidMSISDNError(
            f"Cannot parse phone number '{raw}': {exc}"
        ) from exc

    if not phonenumbers.is_valid_number(parsed):
        raise InvalidMSISDNError(
            f"'{raw}' is not a valid phone number"
        )

    e164 = phonenumbers.format_number(
        parsed, phonenumbers.PhoneNumberFormat.E164
    )
    national = phonenumbers.format_number(
        parsed, phonenumbers.PhoneNumberFormat.NATIONAL
    )
    country_code = f"+{parsed.country_code}"
    country_iso = phonenumbers.region_code_for_number(parsed) or country_hint
    line_type = phonenumbers.number_type(parsed)
    is_mobile = line_type in (
        phonenumbers.PhoneNumberType.MOBILE,
        phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE,
    )

    operator = _detect_operator(parsed, country_iso)

    return MSISDNInfo(
        e164=e164,
        national=national,
        country_code=country_code,
        country_iso=country_iso,
        operator=operator,
        is_mobile=is_mobile,
        is_valid=True,
    )


def normalise_e164(raw: str, *, country_hint: str = "SN") -> str:
    """
    Parse a phone number and return its E.164 representation.

    Convenience wrapper around parse_msisdn() for callers that need
    only the normalised number string and not the full classification.

    Raises InvalidMSISDNError if the number is invalid.
    """
    return parse_msisdn(raw, country_hint=country_hint).e164


def detect_operator(raw: str, *, country_hint: str = "SN") -> str:
    """
    Parse a phone number and return the operator name.

    Returns one of: ORANGE, FREE, UNKNOWN.
    Raises InvalidMSISDNError if the number is invalid.
    """
    return parse_msisdn(raw, country_hint=country_hint).operator


def _detect_operator(
    parsed: phonenumbers.PhoneNumber,
    country_iso: str,
) -> str:
    """
    Internal: classify operator from a parsed PhoneNumber object.

    Uses the national significant number (subscriber number without
    country code) to extract the 2-digit prefix for table lookup.
    Returns UNKNOWN for non-Senegalese numbers or unrecognised prefixes.
    """
    if country_iso != "SN":
        return "UNKNOWN"

    national_significant = phonenumbers.national_significant_number(parsed)
    if len(national_significant) < 2:
        return "UNKNOWN"

    prefix = national_significant[:2]
    return _SN_MOBILE_PREFIXES.get(prefix, "UNKNOWN")