"""
Offset-based pagination utilities.

PaginationParams: FastAPI dependency for skip/limit query parameters.
paginate():       Compute pagination metadata from count + params.
PaginatedResult:  Typed container for paginated list responses.

Usage in route handlers:

    @router.get("/sms/history", response_model=PaginatedResponse[SMSStatusResponse])
    async def list_sms(
        pagination: PaginationParams = Depends(),
        api_key: ApiKey = Depends(require_scope(Scope.SMS_READ)),
        db: AsyncSession = Depends(get_db),
    ):
        repo = SMSRepository(db)
        total = await repo.count_by_application(api_key.application_id)
        items = await repo.list_by_application(
            api_key.application_id,
            skip=pagination.skip,
            limit=pagination.limit,
        )
        return paginate(items, total, pagination)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from fastapi import Query

T = TypeVar("T")

# Hard ceiling on page size — enforced regardless of what the client requests.
# Returning more than MAX_LIMIT rows per page is never correct:
# it creates unbounded response sizes and suggests the client
# should be using a more specific filter, not a bigger page.
MAX_PAGE_SIZE: int = 100
DEFAULT_PAGE_SIZE: int = 20


class PaginationParams:
    """
    FastAPI dependency that parses and validates skip/limit query params.

    Use as: pagination: PaginationParams = Depends()

    Both parameters are validated:
      - skip: minimum 0 (cannot paginate backwards)
      - limit: minimum 1, maximum MAX_PAGE_SIZE (100)

    The limit is capped at MAX_PAGE_SIZE regardless of what the client
    requests — a client asking for limit=10000 gets limit=100.
    """

    def __init__(
        self,
        skip: int = Query(
            default=0,
            ge=0,
            description="Number of records to skip (offset-based pagination)",
        ),
        limit: int = Query(
            default=DEFAULT_PAGE_SIZE,
            ge=1,
            le=MAX_PAGE_SIZE,
            description=f"Number of records to return (max {MAX_PAGE_SIZE})",
        ),
    ) -> None:
        self.skip = skip
        self.limit = limit

    @property
    def page(self) -> int:
        """
        Current page number (1-based) derived from skip and limit.

        Page 1 = skip 0, page 2 = skip limit, page 3 = skip 2×limit.
        This is approximate when skip is not a multiple of limit —
        callers using arbitrary skip values should not rely on this.
        """
        if self.limit == 0:
            return 1
        return (self.skip // self.limit) + 1


@dataclass(frozen=True)
class PaginatedResult(Generic[T]):
    """
    Container for a paginated list of results.

    Immutable after creation — fields are not modified after pagination
    metadata is computed.
    """
    items: list[T]
    total: int
    skip: int
    limit: int
    page: int
    has_next: bool
    has_prev: bool

    @property
    def pages(self) -> int:
        """Total number of pages at this page size."""
        if self.limit == 0 or self.total == 0:
            return 0
        return (self.total + self.limit - 1) // self.limit


def paginate(
    items: list[T],
    total: int,
    params: PaginationParams,
) -> PaginatedResult[T]:
    """
    Compute pagination metadata for a list of results.

    Args:
        items:  The records returned from the repository for this page.
        total:  The total count of records matching the filter
                (from repo.count_by_*() — not len(items)).
        params: PaginationParams from the FastAPI dependency.

    Returns:
        PaginatedResult with items and computed metadata.

    Why total comes from a separate count query and not len(items):
        len(items) is always <= limit, which is <= MAX_PAGE_SIZE.
        The total must reflect the full dataset, not just the current page.
        Correct has_next requires: (skip + limit) < total.
        Using len(items) == limit as has_next is wrong when the last
        page is exactly full — it reports has_next=True when there is
        no next page.
    """
    return PaginatedResult(
        items=items,
        total=total,
        skip=params.skip,
        limit=params.limit,
        page=params.page,
        has_next=(params.skip + params.limit) < total,
        has_prev=params.skip > 0,
    )


def build_pagination_meta(result: PaginatedResult) -> dict:
    """
    Convert a PaginatedResult to the standard pagination metadata dict.

    Used by Pydantic response schemas (PaginatedResponse[T]) to build
    the 'meta' field of paginated API responses.

    Output format:
        {
            "total": 847,
            "page": 2,
            "pages": 43,
            "limit": 20,
            "skip": 20,
            "has_next": true,
            "has_prev": true
        }
    """
    return {
        "total": result.total,
        "page": result.page,
        "pages": result.pages,
        "limit": result.limit,
        "skip": result.skip,
        "has_next": result.has_next,
        "has_prev": result.has_prev,
    }