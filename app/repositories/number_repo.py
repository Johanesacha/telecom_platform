"""
NumberVerification repository.

Records point-in-time MSISDN verification results.

Integration with caching (this repository does NOT touch the cache layer):
  Cache hit  -> NumberService returns cached result, never calls this repo.
  Cache miss -> NumberService calls parse_msisdn(), then this repo's create(),
               then writes the result to the cache layer.

Because the hot path bypasses this repository entirely, its write
throughput is bounded by cache miss rate, not total verification volume.

Audit trail semantics:
  Each verification attempt creates a new record. Existing records are
  never updated. If the same MSISDN is verified 10 times, 10 records exist.
  This provides a complete history for analytics and prefix table drift
  detection (operator assignment changes over time).
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select

from app.domain.number import LineType, NumberVerification, OperatorEnum
from app.repositories.base import BaseRepository


class NumberRepository(BaseRepository[NumberVerification]):

    def __init__(self, session) -> None:
        super().__init__(NumberVerification, session)

    # -- Recent Result Lookup -----------------------------------------------

    async def get_recent_for_msisdn(
        self,
        app_id: UUID,
        msisdn_e164: str,
        *,
        since: datetime,
    ) -> NumberVerification | None:
        """
        Return the most recent valid verification for an MSISDN since
        a given UTC datetime, scoped to one application.

        Called by NumberService after a cache miss to check whether a
        sufficiently recent result exists in the database. If found,
        the service rehydrates the cache from this record rather than
        re-running the phonenumbers library.

        Only verified results (is_valid=True) are returned. An invalid
        number from two minutes ago should not suppress a fresh check
        if the client is retrying with a corrected number.

        Scoped to app_id because verification results are per-application
        records. Two applications verifying the same MSISDN produce
        independent records.

        Returns None if no fresh result exists. The service must then
        run a full verification.
        """
        stmt = (
            select(NumberVerification)
            .where(
                NumberVerification.application_id == app_id,
                NumberVerification.msisdn_e164 == msisdn_e164,
                NumberVerification.is_valid.is_(True),
                NumberVerification.created_at >= since,
            )
            .order_by(NumberVerification.created_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id_for_application(
        self,
        record_id: UUID,
        app_id: UUID,
    ) -> NumberVerification | None:
        """
        Fetch a single verification record by UUID, restricted to
        the calling application's ownership.

        Used by the detail endpoint for a specific verification record.
        Returns None for both 'not found' and 'wrong owner' to prevent
        leaking whether a record with that ID exists at all.
        """
        stmt = (
            select(NumberVerification)
            .where(
                NumberVerification.id == record_id,
                NumberVerification.application_id == app_id,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    # -- Paginated History --------------------------------------------------

    async def list_by_application(
        self,
        app_id: UUID,
        *,
        skip: int = 0,
        limit: int = 20,
        operator_filter: OperatorEnum | None = None,
        valid_only: bool = False,
    ) -> list[NumberVerification]:
        """
        Return paginated verification history for an application.

        Ordered by created_at DESC, most recent verification first.

        Args:
            app_id:          Restrict to this application's records.
            skip:            Pagination offset.
            limit:           Page size. Service layer enforces maximum.
            operator_filter: Optional. Narrow to one operator's numbers.
            valid_only:      True returns only successful verifications.
                             False (default) returns all attempts including
                             invalid numbers, useful for error analytics.
        """
        stmt = (
            select(NumberVerification)
            .where(NumberVerification.application_id == app_id)
            .order_by(NumberVerification.created_at.desc())
            .offset(skip)
            .limit(limit)
        )

        if operator_filter is not None:
            stmt = stmt.where(
                NumberVerification.operator == operator_filter
            )

        if valid_only:
            stmt = stmt.where(NumberVerification.is_valid.is_(True))

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_by_application(
        self,
        app_id: UUID,
        *,
        operator_filter: OperatorEnum | None = None,
        valid_only: bool = False,
    ) -> int:
        """
        Return total verification count for an application.

        Accepts identical filters to list_by_application() for
        consistent count/list pairs in paginated responses.
        """
        stmt = (
            select(func.count())
            .select_from(NumberVerification)
            .where(NumberVerification.application_id == app_id)
        )

        if operator_filter is not None:
            stmt = stmt.where(
                NumberVerification.operator == operator_filter
            )

        if valid_only:
            stmt = stmt.where(NumberVerification.is_valid.is_(True))

        result = await self.session.execute(stmt)
        return result.scalar_one()

    # -- Analytics ---------------------------------------------------------

    async def count_by_operator(
        self,
        app_id: UUID,
        *,
        since: datetime | None = None,
    ) -> dict[str, int]:
        """
        Return verification counts grouped by operator for one application.

        Issues a single GROUP BY query. Used by the monitoring dashboard
        to show operator distribution:

            {"ORANGE": 1203, "FREE": 847, "EXPRESSO": 91, "UNKNOWN": 34}

        The composite index ix_number_app_operator serves this query
        directly. Missing operators are absent from the result.
        The service layer fills in zeros when building response schemas.

        Args:
            app_id: Restrict to this application.
            since:  Optional UTC datetime lower bound on created_at.
        """
        stmt = (
            select(
                NumberVerification.operator,
                func.count().label("total"),
            )
            .where(NumberVerification.application_id == app_id)
            .group_by(NumberVerification.operator)
        )

        if since is not None:
            stmt = stmt.where(NumberVerification.created_at >= since)

        result = await self.session.execute(stmt)
        return {row.operator: row.total for row in result.all()}

    async def count_by_validity(
        self,
        app_id: UUID,
        *,
        since: datetime | None = None,
    ) -> dict[str, int]:
        """
        Return counts of valid vs invalid verification attempts.

        Used to compute the invalid number rate for an application:
            invalid_rate = invalid / (valid + invalid) * 100

        Returns:
            {"valid": 2141, "invalid": 34}

        Both keys are always present. Invalid count is 0 when all
        numbers were valid, not absent from the dict.
        """
        stmt = (
            select(
                NumberVerification.is_valid,
                func.count().label("total"),
            )
            .where(NumberVerification.application_id == app_id)
            .group_by(NumberVerification.is_valid)
        )

        if since is not None:
            stmt = stmt.where(NumberVerification.created_at >= since)

        result = await self.session.execute(stmt)
        rows = {row.is_valid: row.total for row in result.all()}

        return {
            "valid":   rows.get(True, 0),
            "invalid": rows.get(False, 0),
        }

    async def count_by_line_type(
        self,
        app_id: UUID,
    ) -> dict[str, int]:
        """
        Return verification counts grouped by line type.

        Used to show mobile vs fixed vs VOIP distribution:

            {"MOBILE": 2089, "FIXED": 54, "VOIP": 12, "UNKNOWN": 20}

        Missing line types are absent from the result dict.
        """
        stmt = (
            select(
                NumberVerification.line_type,
                func.count().label("total"),
            )
            .where(NumberVerification.application_id == app_id)
            .group_by(NumberVerification.line_type)
        )
        result = await self.session.execute(stmt)
        return {row.line_type: row.total for row in result.all()}