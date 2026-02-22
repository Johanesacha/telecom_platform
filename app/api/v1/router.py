"""
API v1 router — aggregates all endpoint sub-routers.

Registration in main.py:
    app.include_router(api_router, prefix="/api/v1")

This produces the final URL structure:
    /api/v1/health
    /api/v1/auth/register
    /api/v1/auth/token
    /api/v1/keys
    /api/v1/sms/send
    /api/v1/ussd/start
    /api/v1/payments/initiate
    /api/v1/numbers/verify
    /api/v1/notifications/send
    /api/v1/monitoring/dashboard
    /api/v1/quota/usage

Health is registered without a sub-prefix — it lives at /api/v1/health,
not /api/v1/health/health. The health router has no prefix attribute.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import (
    auth,
    health,
    keys,
    monitoring,
    notifications,
    numbers,
    payments,
    quota,
    sms,
    ussd,
)

api_router = APIRouter()

# Health — no prefix (lives at /health relative to the v1 base)
api_router.include_router(health.router)

# Authenticated service endpoints
api_router.include_router(auth.router)           # /auth/...
api_router.include_router(keys.router)           # /keys/...
api_router.include_router(sms.router)            # /sms/...
api_router.include_router(ussd.router)           # /ussd/...
api_router.include_router(payments.router)       # /payments/...
api_router.include_router(numbers.router)        # /numbers/...
api_router.include_router(notifications.router)  # /notifications/...
api_router.include_router(monitoring.router)     # /monitoring/...
api_router.include_router(quota.router)          # /quota/...