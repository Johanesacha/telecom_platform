"""
API Key generation, hashing, and verification.

Key format: tp_live_<64 hex chars>  (total: ~76 chars)
            tp_sand_<64 hex chars>  (sandbox keys)

Storage:
  key_prefix  = first 12 characters (tp_live_ + first 4 of random part)
  key_hash    = SHA-256 of the full raw key (hex digest, 64 chars)
  raw_key     = returned ONCE to the developer, NEVER stored

Authentication flow:
  1. Client sends X-API-Key: tp_live_<random>
  2. Extract prefix (first 12 chars)
  3. DB lookup WHERE key_prefix = prefix (fast indexed lookup)
  4. Compare hash of provided key with stored hash via secrets.compare_digest()
  5. If match: authenticated. If no match: same error as 'not found' (no timing oracle)
"""
from __future__ import annotations

import hashlib
import secrets


LIVE_PREFIX = "tp_live_"
SAND_PREFIX = "tp_sand_"
PREFIX_LENGTH = 12


def generate_api_key(sandbox: bool = False) -> tuple[str, str, str]:
    """
    Generate a new API key.

    Returns:
        (raw_key, key_prefix, key_hash)
        raw_key   — return to developer ONCE, never store
        key_prefix — store in DB for fast lookup
        key_hash  — store in DB for verification
    """
    type_prefix = SAND_PREFIX if sandbox else LIVE_PREFIX
    random_part = secrets.token_hex(32)
    raw_key = type_prefix + random_part

    key_prefix = raw_key[:PREFIX_LENGTH]
    key_hash = _hash_key(raw_key)

    return raw_key, key_prefix, key_hash


def _hash_key(raw_key: str) -> str:
    """SHA-256 hash of the raw API key. Returns 64-char hex string."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def verify_api_key(raw_key: str, stored_hash: str) -> bool:
    """
    Verify a raw API key against its stored SHA-256 hash.

    CRITICAL: Use secrets.compare_digest() — not ==.
    secrets.compare_digest() runs in constant time regardless of input.
    Using == would allow timing attacks.
    """
    computed_hash = _hash_key(raw_key)
    return secrets.compare_digest(computed_hash, stored_hash)


def extract_prefix(raw_key: str) -> str:
    """Extract the key prefix (first 12 chars) for database lookup."""
    if len(raw_key) < PREFIX_LENGTH:
        return ""
    return raw_key[:PREFIX_LENGTH]


def is_sandbox_key(raw_key: str) -> bool:
    """Determine if a key is a sandbox key from its prefix."""
    return raw_key.startswith(SAND_PREFIX)