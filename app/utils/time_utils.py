"""
Timezone-safe datetime utilities.
Always use utcnow() — never datetime.now() which returns local time.
"""
from datetime import datetime, timezone, timedelta


def utcnow() -> datetime:
    """Return current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


def utcnow_plus(seconds: int = 0, minutes: int = 0, days: int = 0) -> datetime:
    """Return UTC datetime offset by the given duration."""
    delta = timedelta(seconds=seconds, minutes=minutes, days=days)
    return utcnow() + delta


def today_utc_str() -> str:
    """Return today's UTC date as YYYY-MM-DD string. Used as Redis quota key component."""
    return utcnow().strftime("%Y-%m-%d")