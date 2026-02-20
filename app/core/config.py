"""
Application configuration via pydantic-settings.
All settings are loaded from environment variables / .env file at startup.
Type validation happens before any route is registered.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    environment: str = "development"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    project_name: str = "Telecom API Platform"
    api_version: str = "1.0.0"

    # Database
    database_url: str
    sync_database_url: str

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # JWT
    jwt_private_key_path: str = "./jwt_private.pem"
    jwt_public_key_path: str = "./jwt_public.pem"
    jwt_algorithm: str = "RS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # Rate Limiting
    rate_limit_burst_free: int = 10
    rate_limit_burst_standard: int = 60
    rate_limit_burst_premium: int = 500
    rate_limit_burst_sandbox: int = 120

    # Quotas
    quota_free_sms: int = 100
    quota_free_payments: int = 20
    quota_standard_sms: int = 1000
    quota_standard_payments: int = 200
    quota_premium_sms: int = 10000
    quota_premium_payments: int = 2000

    # USSD
    ussd_session_ttl_seconds: int = 180

    # Admin initial
    initial_admin_email: str = "admin@telecom.sn"
    initial_admin_password: str = "CHANGE_ME"

    @model_validator(mode="after")
    def validate_jwt_keys_exist(self) -> "Settings":
        if self.environment != "test":
            private_path = Path(self.jwt_private_key_path)
            public_path = Path(self.jwt_public_key_path)
            if not private_path.exists():
                raise ValueError(
                    f"JWT private key not found: {private_path}. "
                    "Run: openssl genrsa -out jwt_private.pem 2048"
                )
            if not public_path.exists():
                raise ValueError(
                    f"JWT public key not found: {public_path}. "
                    "Run: openssl rsa -in jwt_private.pem -pubout -out jwt_public.pem"
                )
        return self

    @field_validator("jwt_algorithm")
    @classmethod
    def algorithm_must_be_rs256(cls, v: str) -> str:
        if v != "RS256":
            raise ValueError(
                f"JWT algorithm must be RS256 (asymmetric). Got: {v}. "
                "HS256 is insecure for this architecture."
            )
        return v

    @property
    def jwt_private_key(self) -> str:
        return Path(self.jwt_private_key_path).read_text()

    @property
    def jwt_public_key(self) -> str:
        return Path(self.jwt_public_key_path).read_text()

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    """
    Return cached Settings instance.
    Use this function everywhere instead of instantiating Settings() directly.
    lru_cache ensures .env is read only once per process lifetime.
    """
    return Settings()


settings = get_settings()