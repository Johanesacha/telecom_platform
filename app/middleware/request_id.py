"""
RequestID Middleware — must be registered LAST in main.py (executes first).

Responsibilities:
  1. Read X-Request-ID from incoming request header if present.
     Allows clients to inject their own correlation ID for end-to-end tracing.
  2. Generate a UUID4 if no X-Request-ID is provided.
  3. Store the ID in request.state.request_id — consumed by:
       - AuditMiddleware (reads after call_next)
       - Route handlers via request.state.request_id
       - ApiMeta.build(request.state.request_id) in every response
  4. Echo the ID back in the X-Request-ID response header — clients
     always know the request ID regardless of whether they generated it.

Zero external app dependencies:
  This module imports only stdlib (uuid) and Starlette.
  It must remain importable even if the rest of the application
  fails to initialise — it is the outermost layer.

Header format:
  X-Request-ID must be a valid UUID4 string when generated here.
  Client-supplied values are accepted as-is (any non-empty string).
  Maximum length is not enforced — downstream systems should truncate
  at their own boundary if needed.
"""
from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

_HEADER_NAME = "X-Request-ID"
_MAX_CLIENT_ID_LENGTH = 128  # reject absurdly long client-supplied IDs


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Inject and propagate a unique request correlation ID.

    Registration in main.py (executes first because added last):
        app.add_middleware(RequestIDMiddleware)  # last added → outermost
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        # Read client-supplied ID or generate a fresh one
        client_id = request.headers.get(_HEADER_NAME, "").strip()

        if client_id and len(client_id) <= _MAX_CLIENT_ID_LENGTH:
            request_id = client_id
        else:
            # Generate UUID4 — cryptographically random, collision-resistant
            request_id = str(uuid.uuid4())

        # Store in request.state — accessible by all downstream middleware,
        # dependencies, and route handlers for this request lifecycle.
        request.state.request_id = request_id

        # Process request through the remaining middleware chain and handler
        response = await call_next(request)

        # Echo back — clients always know the correlation ID
        response.headers[_HEADER_NAME] = request_id

        return response