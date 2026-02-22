"""
FastAPI application factory.

Startup sequence:
  1. create_app() is called (by uvicorn or test client)
  2. FastAPI registers lifespan context manager
  3. On first request: lifespan startup runs
       - Redis connection pool created and stored in app.state.redis
       - Startup logged
  4. Requests flow through middleware stack (outer → inner):
       RequestIDMiddleware → AuditMiddleware → RateLimitMiddleware → route
  5. On shutdown: lifespan cleanup runs
       - Redis pool gracefully closed

Middleware registration order vs execution order:
  add_middleware() registers in a stack — LAST added is OUTERMOST (runs first).
  Registration order (first call → innermost):
    1. add_middleware(RateLimitMiddleware)   → innermost, runs last pre-handler
    2. add_middleware(AuditMiddleware)       → middle
    3. add_middleware(RequestIDMiddleware)   → outermost, runs first
    4. add_middleware(CORSMiddleware)        → even more outer (Starlette built-in)

  CORSMiddleware is added last so it wraps everything, including OPTIONS
  preflight requests that never touch authentication middleware.

Exception handlers:
  HTTPException:           4xx/5xx raised by FastAPI or route handlers
  RequestValidationError:  422 from Pydantic schema validation failure
  Exception:               catch-all for unexpected 500 errors

  All three return ApiResponse.fail() envelope to maintain consistent
  response shape. Clients always parse the same JSON structure.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.middleware.audit import AuditMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_id import RequestIDMiddleware
from app.api.v1.router import api_router
from app.schemas.common import ApiResponse, ErrorDetail, ApiMeta

logger = logging.getLogger(__name__)


# ── Lifespan ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Manage application-level resources.

    Startup:
      Create the shared aioredis connection pool and attach it to app.state.
      max_connections=20 supports 20 concurrent Redis commands across all
      requests without waiting for a free connection (adequate at this scale).
      decode_responses=True means all Redis values are returned as str,
      never bytes — consistent with the rest of the codebase.

    Shutdown:
      aclose() sends QUIT to Redis and waits for in-flight commands to finish.
      This prevents 'Connection reset by peer' errors in Redis server logs.
    """
    # ── Startup ────────────────────────────────────────────────────────────
    logger.info("Telecom Platform API starting up...")

    redis_pool = aioredis.from_url(
        str(settings.redis_url),
        max_connections=20,
        encoding="utf-8",
        decode_responses=True,
    )
    app.state.redis = redis_pool
    logger.info("Redis connection pool initialised (max_connections=20)")

    logger.info(
        "Telecom Platform API ready",
        extra={
            "docs": "/docs",
            "redoc": "/redoc",
            "health": "/api/v1/health",
        },
    )

    yield  # application runs here

    # ── Shutdown ───────────────────────────────────────────────────────────
    logger.info("Telecom Platform API shutting down...")
    await redis_pool.aclose()
    logger.info("Redis connection pool closed")
    logger.info("Shutdown complete")


# ── Error response helpers ─────────────────────────────────────────────────

def _request_id(request: Request) -> str:
    """
    Safely extract request_id from request.state.

    RequestIDMiddleware sets this on every request. The getattr guard
    handles the rare case where an exception fires before any middleware
    runs (e.g. during application startup health check misconfiguration).
    """
    return getattr(request.state, "request_id", "unknown")


def _error_json(
    *,
    request: Request,
    status_code: int,
    code: str,
    message: str,
    field: str | None = None,
) -> JSONResponse:
    """
    Build a JSONResponse with the ApiResponse error envelope.

    All exception handlers funnel through here so the response shape
    is identical regardless of which handler triggered.
    """
    body = ApiResponse.fail(
        code=code,
        message=message,
        request_id=_request_id(request),
        field=field,
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(),
    )


# ── Exception handlers ─────────────────────────────────────────────────────

async def _http_exception_handler(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    """
    Handle FastAPI HTTPException (401, 403, 404, 422, 429, etc.).

    Maps HTTP status codes to machine-readable error codes:
      401 → AUTH_001 (unauthenticated)
      403 → AUTH_003 (forbidden)
      404 → NOT_FOUND
      429 → RATE_001 (already handled by RateLimitMiddleware — belt-and-suspenders)
      4xx → CLIENT_ERROR
      5xx → SERVER_ERROR
    """
    status_to_code: dict[int, str] = {
        status.HTTP_400_BAD_REQUEST:          "VALIDATION_001",
        status.HTTP_401_UNAUTHORIZED:         "AUTH_001",
        status.HTTP_403_FORBIDDEN:            "AUTH_003",
        status.HTTP_404_NOT_FOUND:            "NOT_FOUND",
        status.HTTP_405_METHOD_NOT_ALLOWED:   "METHOD_NOT_ALLOWED",
        status.HTTP_409_CONFLICT:             "CONFLICT",
        status.HTTP_422_UNPROCESSABLE_ENTITY: "VALIDATION_422",
        status.HTTP_429_TOO_MANY_REQUESTS:    "RATE_001",
        status.HTTP_500_INTERNAL_SERVER_ERROR:"SERVER_500",
        status.HTTP_503_SERVICE_UNAVAILABLE:  "SERVER_503",
    }
    code = status_to_code.get(exc.status_code, "CLIENT_ERROR")

    response = _error_json(
        request=request,
        status_code=exc.status_code,
        code=code,
        message=str(exc.detail) if exc.detail else "An error occurred",
    )

    # Preserve headers set by the raising code (e.g. WWW-Authenticate: Bearer)
    if exc.headers:
        response.headers.update(exc.headers)

    return response


async def _validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """
    Handle Pydantic v2 RequestValidationError (422 Unprocessable Entity).

    Takes the first validation error and maps it to the field/message
    convention used by ErrorDetail. Developers see which field failed
    and why — precise enough to fix their request without guessing.
    """
    errors = exc.errors()
    if errors:
        first = errors[0]
        # loc is a tuple: ('body', 'to_number') or ('body',) for top-level
        loc = first.get("loc", ())
        field = str(loc[-1]) if len(loc) > 1 else None
        message = first.get("msg", "Validation error")
        # Pydantic v2 prefixes messages with "Value error, " for field_validators
        message = message.replace("Value error, ", "")
    else:
        field = None
        message = "Request validation failed"

    return _error_json(
        request=request,
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        code="VALIDATION_422",
        message=message,
        field=field,
    )


async def _generic_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """
    Catch-all for unhandled exceptions.

    Logs the full traceback for the operations team.
    Returns a vague 500 message to the client — never leak internal details.
    """
    logger.exception(
        "Unhandled exception on %s %s",
        request.method,
        request.url.path,
        extra={"request_id": _request_id(request)},
    )
    return _error_json(
        request=request,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="SERVER_500",
        message=(
            "An unexpected error occurred. "
            "The error has been logged. "
            f"Reference your request ID for support: {_request_id(request)}"
        ),
    )


# ── Application factory ────────────────────────────────────────────────────

def create_app() -> FastAPI:
    """
    Construct and configure the FastAPI application.

    Returns a fully configured FastAPI instance ready for uvicorn.
    This factory pattern enables clean test client creation:
        from app.main import create_app
        app = create_app()
        client = TestClient(app)

    Middleware registration order (first = innermost):
      1. RateLimitMiddleware  — innermost, runs last before route handler
      2. AuditMiddleware      — middle, reads api_key after call_next
      3. RequestIDMiddleware  — outermost app middleware, runs first
      4. CORSMiddleware       — Starlette built-in, wraps everything
    """
    app = FastAPI(
        title="Telecom Platform API",
        version="1.0.0",
        description=(
            "Telecom services platform providing SMS, USSD, Mobile Money, "
            "Number Verification, and Notification APIs for developers in "
            "Senegal and West Africa."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # ── Middleware (first added = innermost) ───────────────────────────────
    # RateLimitMiddleware: sliding window check BEFORE auth dependency runs.
    # Reads raw Authorization header directly (auth dep not yet executed).
    app.add_middleware(RateLimitMiddleware)

    # AuditMiddleware: logs every request AFTER response.
    # Reads request.state.api_key which is set by auth dep during call_next.
    app.add_middleware(AuditMiddleware)

    # RequestIDMiddleware: outermost app middleware — runs before everything.
    # Injects X-Request-ID into request.state and echoes it in response.
    app.add_middleware(RequestIDMiddleware)

    # CORSMiddleware: Starlette built-in, wraps the entire app.
    # Handles OPTIONS preflight before any of our middleware sees the request.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            "X-Request-ID",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
        ],
    )

    # ── Exception handlers ─────────────────────────────────────────────────
    # Registered in specificity order: most specific first.
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(HTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _generic_exception_handler)

    # ── Router ─────────────────────────────────────────────────────────────
    # All API routes live under /api/v1/.
    # /api/v1/health, /api/v1/sms/send, /api/v1/payments/initiate, etc.
    app.include_router(api_router, prefix="/api/v1")

    return app


# ── ASGI entrypoint ────────────────────────────────────────────────────────
# uvicorn app.main:app
# gunicorn -k uvicorn.workers.UvicornWorker app.main:app
app = create_app()