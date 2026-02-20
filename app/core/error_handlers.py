"""
Map exceptions to standardized JSON responses.
All error responses follow the same envelope: {success, error, meta}.
Stack traces are NEVER exposed in responses.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.core.exceptions import TelecomPlatformError, RateLimitExceededError

logger = logging.getLogger(__name__)


def _meta() -> dict:
    return {
        "request_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "api_version": "1.0.0",
    }


def register_exception_handlers(app: FastAPI) -> None:
    """Register all exception handlers on the FastAPI application."""

    @app.exception_handler(TelecomPlatformError)
    async def telecom_error_handler(
        request: Request, exc: TelecomPlatformError
    ) -> JSONResponse:
        headers = {}
        if isinstance(exc, RateLimitExceededError):
            headers["Retry-After"] = str(exc.retry_after)
        logger.warning(
            "Business error",
            extra={"error_code": exc.error_code, "path": request.url.path},
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "success": False,
                "error": {"code": exc.error_code, "message": exc.message},
                "meta": _meta(),
            },
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        first_error = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(x) for x in first_error.get("loc", [])[1:])
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": {
                    "code": "VAL_001",
                    "message": first_error.get("msg", "Validation error"),
                    "field": field or None,
                },
                "meta": _meta(),
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.exception(
            "Unhandled exception",
            extra={"path": request.url.path, "method": request.method},
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": {"code": "GEN_001", "message": "An unexpected error occurred"},
                "meta": _meta(),
            },
        )