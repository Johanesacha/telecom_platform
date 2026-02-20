"""
Password hashing and verification using bcrypt via Passlib.
bcrypt is adaptive: cost factor (rounds=12) makes GPU brute-force impractical.
Never use hashlib.md5/sha256 for passwords — they are too fast.
"""
from passlib.context import CryptContext


_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


def hash_password(plain_password: str) -> str:
    """
    Return bcrypt hash of the password.
    The hash includes the salt — do not store the salt separately.
    """
    return _pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a plaintext password against a bcrypt hash.
    Returns True if they match, False otherwise.
    This function takes ~250ms intentionally — brute-force protection.
    """
    return _pwd_context.verify(plain_password, hashed_password)