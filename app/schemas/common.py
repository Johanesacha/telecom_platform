"""
Common response schemas used by every endpoint in the platform.

Import hierarchy:
  All other schema files import from this module.
  This module imports nothing from other schemas — zero circular imports possible.

Build order requirement:
  This file must exist and validate before any other schema file is written.
  Every route handler wraps its return value in ApiResponse[T] or PaginatedResponse[T].

Generic design:
  ApiResponse[T] and PaginatedResponse[T] are Generic[T] with T bound to BaseModel.
  Pydantic v2 propagates the inner type into OpenAPI schema generation.
  The generated OpenAPI spec is precisely typed — no 'any' data fields.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.utils.time_utils import utcnow

# T is bound to BaseModel: only Pydantic models are valid as response data.
# Primitive types (str, int, dict) are explicitly excluded.
T = TypeVar("T", bound=BaseModel)


class ApiMeta(BaseModel):
    """
    Metadata attached to every API response.

    request_id: echoed from X-Request-ID header set by RequestIDMiddleware.
                Allows developers to correlate responses to specific requests
                in their own logs and in the platform's audit trail.

    timestamp:  UTC time when the response was generated.
                ISO 8601 with timezone suffix — never naive datetime.

    version:    API version string. Clients can use this to detect version
                changes without parsing the URL path.
    """
    model_config = ConfigDict(frozen=True)

    request_id: str = Field(
        description="Unique request identifier — correlates to X-Request-ID header"
    )
    timestamp: str = Field(
        description="UTC timestamp when this response was generated (ISO 8601)"
    )
    version: str = Field(
        default="1.0",
        description="API version"
    )

    @classmethod
    def build(cls, request_id: str) -> "ApiMeta":
        """
        Construct ApiMeta with current UTC timestamp.

        Convenience factory used by route handlers:
            meta = ApiMeta.build(request.state.request_id)
        """
        return cls(
            request_id=request_id,
            timestamp=utcnow().isoformat(),
            version="1.0",
        )


class ErrorDetail(BaseModel):
    """
    Structured error information returned on all non-2xx responses.

    code:    Machine-readable error code string (e.g. AUTH_001, RATE_001).
             Clients should branch on code, not on message — messages may
             change between API versions, codes are stable.

    message: Human-readable description of the error.
             Safe to display to end-users — no stack traces, no internal paths.

    field:   Optional field name for validation errors (422 responses).
             Null for non-validation errors (auth failures, rate limits, etc.).
    """
    model_config = ConfigDict(frozen=True)

    code: str = Field(description="Machine-readable error code")
    message: str = Field(description="Human-readable error description")
    field: str | None = Field(
        default=None,
        description="Field name for validation errors — null for other error types"
    )


class ApiResponse(BaseModel, Generic[T]):
    """
    Standard API response envelope for all single-object responses.

    Every endpoint that returns a single object wraps it in ApiResponse[T]:
        return ApiResponse[UserResponse](
            success=True,
            data=user_response,
            meta=ApiMeta.build(request_id),
        )

    On success: success=True,  data=<T instance>, error=None
    On failure: success=False, data=None,          error=<ErrorDetail>

    The error_handlers.py module builds failure responses:
        return ApiResponse[None](
            success=False,
            data=None,
            error=ErrorDetail(code="AUTH_001", message="..."),
            meta=ApiMeta.build(request_id),
        )
    """
    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
    )

    success: bool = Field(description="True on 2xx responses, False on all errors")
    data: T | None = Field(default=None, description="Response payload on success")
    error: ErrorDetail | None = Field(default=None, description="Error details on failure")
    meta: ApiMeta = Field(description="Request metadata")

    @classmethod
    def ok(cls, data: T, *, request_id: str) -> "ApiResponse[T]":
        """
        Build a successful response.

        Usage:
            return ApiResponse.ok(user_schema, request_id=req.state.request_id)
        """
        return cls(
            success=True,
            data=data,
            error=None,
            meta=ApiMeta.build(request_id),
        )

    @classmethod
    def fail(
        cls,
        *,
        code: str,
        message: str,
        request_id: str,
        field: str | None = None,
    ) -> "ApiResponse[None]":
        """
        Build an error response.

        Used by error_handlers.py — not typically called from route handlers.
        """
        return cls(
            success=False,
            data=None,
            error=ErrorDetail(code=code, message=message, field=field),
            meta=ApiMeta.build(request_id),
        )


class PaginationMeta(BaseModel):
    """
    Pagination metadata included in every paginated list response.

    Clients use has_next / has_prev to determine whether to show
    navigation controls. total enables "showing X of Y" UI patterns.
    page and pages enable page-jump navigation.
    """
    model_config = ConfigDict(frozen=True)

    total: int = Field(description="Total number of records matching the filter")
    page: int = Field(description="Current page number (1-based)")
    pages: int = Field(description="Total number of pages at the current limit")
    limit: int = Field(description="Records per page (max 100)")
    skip: int = Field(description="Records skipped (offset)")
    has_next: bool = Field(description="True if a next page exists")
    has_prev: bool = Field(description="True if a previous page exists")

    @classmethod
    def from_paginated_result(cls, result) -> "PaginationMeta":
        """
        Build from a PaginatedResult instance (app/utils/pagination.py).

        Usage in route handler:
            paginated = paginate(items, total, params)
            meta = PaginationMeta.from_paginated_result(paginated)
        """
        return cls(
            total=result.total,
            page=result.page,
            pages=result.pages,
            limit=result.limit,
            skip=result.skip,
            has_next=result.has_next,
            has_prev=result.has_prev,
        )


class PaginatedResponse(BaseModel, Generic[T]):
    """
    Standard API response envelope for all paginated list responses.

    Every list endpoint uses this:
        return PaginatedResponse[SMSStatusResponse](
            success=True,
            items=[SMSStatusResponse.model_validate(m) for m in messages],
            pagination=PaginationMeta.from_paginated_result(paginated),
            meta=ApiMeta.build(request_id),
        )

    Structurally parallel to ApiResponse[T] but with:
      - items: list[T] instead of data: T
      - pagination: PaginationMeta for navigation metadata
    """
    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
    )

    success: bool = Field(default=True)
    items: list[T] = Field(description="List of records for this page")
    pagination: PaginationMeta = Field(description="Pagination metadata")
    meta: ApiMeta = Field(description="Request metadata")

    @classmethod
    def ok(
        cls,
        items: list[T],
        *,
        pagination: PaginationMeta,
        request_id: str,
    ) -> "PaginatedResponse[T]":
        """
        Build a successful paginated response.

        Usage:
            return PaginatedResponse.ok(
                items=schemas,
                pagination=PaginationMeta.from_paginated_result(result),
                request_id=req.state.request_id,
            )
        """
        return cls(
            success=True,
            items=items,
            pagination=pagination,
            meta=ApiMeta.build(request_id),
        )