"""
Audit Middleware — logs every HTTP request to ApiCallLog after response.

Execution position: MIDDLE of the middleware stack.
  - Runs AFTER RequestIDMiddleware has set request.state.request_id
  - Logs AFTER call_next() so request.state.api_key is available
    (auth dependency sets it during call_next execution)
  - Runs BEFORE the response reaches RateLimitMiddleware

Session contract:
  Creates a FRESH AsyncSession per request, independent of the route
  handler's session. If the handler's session was rolled back, the
  audit session is unaffected — audit records are never lost due to
  business logic failures.

  AuditService.log_request() commits its own session.
  This middleware closes the session via async context manager.

Timing:
  response_time_ms = monotonic time from dispatch() start to
  call_next() completion. This includes route handler time,
  auth dependency time, and all inner middleware time.
  It does NOT include the time taken by this audit log write itself.

api_key access:
  request.state.api_key is set by the auth dependency DURING call_next().
  It is available here AFTER call_next() returns.
  It may be None for unauthenticated routes (health, docs) or for
  requests that failed authentication — both are logged correctly.

Failure handling:
  Audit log failure is non-critical. A logging failure must never
  cause an API response to change (no 500 due to audit failure).
  Errors are caught and logged to stderr — the original response
  is always returned.
"""
from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

# Path segment → service_type string mapping
# Checked against each segment of the URL path
_SERVICE_TYPE_MAP: dict[str, str] = {
    "sms": "sms",
    "payments": "payments",
    "ussd": "ussd",
    "numbers": "numbers",
    "notifications": "notifications",
    "keys": "api_keys",
    "auth": "auth",
    "monitoring": "monitoring",
    "quota": "quota",
}


def _detect_service_type(path: str) -> str | None:
    """
    Infer the service_type from the request URL path.

    Splits path into segments and checks each against the service map.
    Returns the first match, or None for health/docs/unrecognised paths.

    Examples:
      '/api/v1/sms/send'           → 'sms'
      '/api/v1/payments/initiate'  → 'payments'
      '/api/v1/ussd/start'         → 'ussd'
      '/health'                    → None
      '/docs'                      → None
    """
    segments = path.strip("/").lower().split("/")
    for segment in segments:
        if segment in _SERVICE_TYPE_MAP:
            return _SERVICE_TYPE_MAP[segment]
    return None


class AuditMiddleware(BaseHTTPMiddleware):
    """
    Log every HTTP request after the response is produced.

    Registration in main.py:
        app.add_middleware(AuditMiddleware)  # second from last added → middle
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.monotonic()
        status_code = 500  # default if call_next raises

        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            # Log in finally so we capture failures too.
            # Audit failure must never alter the response — exceptions suppressed.
            try:
                await self._log_request(request, status_code, elapsed_ms)
            except Exception:
                logger.exception(
                    "AuditMiddleware: failed to write audit log — non-critical",
                    extra={
                        "path": request.url.path,
                        "status_code": status_code,
                    },
                )

    async def _log_request(
        self,
        request: Request,
        status_code: int,
        response_time_ms: int,
    ) -> None:
        """
        Write one ApiCallLog entry via AuditService.

        Creates a fresh AsyncSession — never shares with the route handler.
        AuditService.log_request() commits the session before returning.
        """
        from app.core.database import AsyncSessionLocal
        from app.services.audit_service import AuditService

        # Extract application context from request state
        # api_key is set by auth dependency during call_next — available here
        api_key = getattr(request.state, "api_key", None)

        application_id: str | None = None
        is_sandbox: bool = False

        if api_key is not None:
            # application_id is a UUID column — serialise to str for audit log
            raw_app_id = getattr(api_key, "application_id", None)
            if raw_app_id is not None:
                application_id = str(raw_app_id)

            # Detect sandbox from key_type enum
            key_type = getattr(api_key, "key_type", None)
            if key_type is not None:
                key_type_str = (
                    key_type.value if hasattr(key_type, "value") else str(key_type)
                )
                is_sandbox = key_type_str.upper() == "SANDBOX"

        # Extract client IP — check X-Forwarded-For first (reverse proxy)
        ip_address: str | None = (
            request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.headers.get("X-Real-IP")
            or getattr(request.client, "host", None)
        ) or None

        request_id: str = getattr(request.state, "request_id", "unknown")
        service_type = _detect_service_type(request.url.path)

        async with AsyncSessionLocal() as session:
            audit_svc = AuditService(session)
            await audit_svc.log_request(
                request_id=request_id,
                endpoint=str(request.url.path),
                method=request.method,
                status_code=status_code,
                response_time_ms=response_time_ms,
                application_id=application_id,
                service_type=service_type,
                is_sandbox=is_sandbox,
                ip_address=ip_address,
            )
            # Safety commit — AuditService.log_request() commits its own
            # session per design, but we add this as a belt-and-suspenders
            # guard. commit() on an already-committed session is a no-op.
            await session.commit()