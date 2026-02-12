"""Exchange audit logger — immutable transaction audit trail.

Issue #1360 Phase 1 & 2: Records every exchange transaction with
cryptographic integrity (SHA-256 self-hash) and provides cursor-based
query/export capabilities.

Immutability is enforced at two levels:
1. ORM event guard: SQLAlchemy ``before_update`` / ``before_delete``
2. PostgreSQL trigger (see migration in Step 6)

Write performance: callers should use ``asyncio.create_task()`` to
fire-and-forget audit writes so they never block the hot path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, event, func, select
from sqlalchemy.orm import Session

from nexus.storage.models.exchange_audit_log import ExchangeAuditLogModel
from nexus.storage.query_mixin import AppendOnlyQueryMixin

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Immutability guards (Decision #3)
# ---------------------------------------------------------------------------


@event.listens_for(ExchangeAuditLogModel, "before_update")
def _reject_update(mapper: Any, connection: Any, target: Any) -> None:  # noqa: ARG001
    """Prevent any UPDATE on audit log records at the ORM level."""
    raise RuntimeError("Exchange audit log records are immutable: UPDATE not allowed")


@event.listens_for(ExchangeAuditLogModel, "before_delete")
def _reject_delete(mapper: Any, connection: Any, target: Any) -> None:  # noqa: ARG001
    """Prevent any DELETE on audit log records at the ORM level."""
    raise RuntimeError("Exchange audit log records are immutable: DELETE not allowed")


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------


def _normalize_amount(amount: Decimal | str) -> str:
    """Normalize amount to 6 decimal places for deterministic hashing.

    Ensures ``Decimal('10')`` and ``Decimal('10.000000')`` produce the
    same canonical string (``'10.000000'``), since the DB ``Numeric(18,6)``
    normalizes stored values to 6 decimal places.
    """
    d = Decimal(str(amount))
    return f"{d:.6f}"


def compute_record_hash(
    *,
    protocol: str,
    buyer_agent_id: str,
    seller_agent_id: str,
    amount: Decimal | str,
    currency: str,
    status: str,
    application: str,
    zone_id: str,
    trace_id: str | None,
    transfer_id: str | None,
    created_at: datetime,
) -> str:
    """Compute SHA-256 hash over canonical field representation.

    The hash covers every business-relevant field so that any
    post-hoc tampering is detectable via ``verify_integrity``.
    """
    canonical = "|".join(
        [
            protocol,
            buyer_agent_id,
            seller_agent_id,
            _normalize_amount(amount),
            currency,
            status,
            application,
            zone_id,
            trace_id or "",
            transfer_id or "",
            created_at.isoformat(),
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_metadata_hash(metadata: dict[str, Any] | None) -> str | None:
    """SHA-256 of deterministically-serialized metadata, or None."""
    if not metadata:
        return None
    serialized = json.dumps(metadata, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _build_merkle_root(hashes: list[str]) -> str:
    """Build a Merkle root from a list of hex-encoded SHA-256 hashes.

    Uses a standard binary Merkle tree construction.
    If the number of leaves is odd, the last leaf is duplicated.
    """
    if not hashes:
        return hashlib.sha256(b"").hexdigest()

    current_level = list(hashes)
    while len(current_level) > 1:
        next_level: list[str] = []
        for i in range(0, len(current_level), 2):
            left = current_level[i]
            right = current_level[i + 1] if i + 1 < len(current_level) else left
            combined = hashlib.sha256((left + right).encode("utf-8")).hexdigest()
            next_level.append(combined)
        current_level = next_level

    return current_level[0]


# ---------------------------------------------------------------------------
# ExchangeAuditLogger
# ---------------------------------------------------------------------------


class ExchangeAuditLogger:
    """Append-only exchange transaction logger with query capabilities.

    Args:
        session_factory: Callable that returns a fresh SQLAlchemy Session.
    """

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory
        self._query = AppendOnlyQueryMixin(
            model_class=ExchangeAuditLogModel,
            id_column_name="id",
            created_column_name="created_at",
        )

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def record(
        self,
        *,
        protocol: str,
        buyer_agent_id: str,
        seller_agent_id: str,
        amount: Decimal,
        currency: str = "credits",
        status: str,
        application: str,
        zone_id: str = "default",
        trace_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        transfer_id: str | None = None,
    ) -> str:
        """Record an exchange transaction (append-only).

        Returns:
            The record ID (UUID string).
        """
        now = datetime.now(UTC)
        record_hash = compute_record_hash(
            protocol=protocol,
            buyer_agent_id=buyer_agent_id,
            seller_agent_id=seller_agent_id,
            amount=amount,
            currency=currency,
            status=status,
            application=application,
            zone_id=zone_id,
            trace_id=trace_id,
            transfer_id=transfer_id,
            created_at=now,
        )
        metadata_h = compute_metadata_hash(metadata)

        row = ExchangeAuditLogModel(
            record_hash=record_hash,
            created_at=now,
            protocol=protocol,
            buyer_agent_id=buyer_agent_id,
            seller_agent_id=seller_agent_id,
            amount=amount,
            currency=currency,
            status=status,
            application=application,
            zone_id=zone_id,
            trace_id=trace_id,
            metadata_hash=metadata_h,
            transfer_id=transfer_id,
        )

        session = self._session_factory()
        try:
            session.add(row)
            session.flush()
            record_id: str = row.id
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        return record_id

    # ------------------------------------------------------------------
    # Read / Query
    # ------------------------------------------------------------------

    def get_transaction(self, record_id: str) -> ExchangeAuditLogModel | None:
        """Get a single transaction by ID."""
        session = self._session_factory()
        try:
            stmt = select(ExchangeAuditLogModel).where(ExchangeAuditLogModel.id == record_id)
            return session.execute(stmt).scalar_one_or_none()
        finally:
            session.close()

    def list_transactions_cursor(
        self,
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[ExchangeAuditLogModel], str | None]:
        """Query transactions with cursor-based pagination."""
        session = self._session_factory()
        try:
            return self._query.list_cursor(session, filters=filters, limit=limit, cursor=cursor)
        finally:
            session.close()

    def count_transactions(self, **filters: Any) -> int:
        """Count matching transactions."""
        session = self._session_factory()
        try:
            return self._query.count(session, filters=filters)
        finally:
            session.close()

    def get_aggregations(
        self,
        *,
        zone_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, Any]:
        """Compute aggregations: total_volume, tx_count, top counterparties."""
        session = self._session_factory()
        try:
            base_filters = [ExchangeAuditLogModel.zone_id == zone_id]
            if since is not None:
                base_filters.append(ExchangeAuditLogModel.created_at >= since)
            if until is not None:
                base_filters.append(ExchangeAuditLogModel.created_at <= until)

            # Total volume + count
            stmt = select(
                func.coalesce(func.sum(ExchangeAuditLogModel.amount), 0),
                func.count(),
            ).where(*base_filters)
            total_volume, tx_count = session.execute(stmt).one()

            # Top buyers
            buyer_stmt = (
                select(
                    ExchangeAuditLogModel.buyer_agent_id,
                    func.sum(ExchangeAuditLogModel.amount).label("volume"),
                )
                .where(*base_filters)
                .group_by(ExchangeAuditLogModel.buyer_agent_id)
                .order_by(desc("volume"))
                .limit(10)
            )
            top_buyers = [
                {"agent_id": row[0], "volume": str(row[1])} for row in session.execute(buyer_stmt)
            ]

            # Top sellers
            seller_stmt = (
                select(
                    ExchangeAuditLogModel.seller_agent_id,
                    func.sum(ExchangeAuditLogModel.amount).label("volume"),
                )
                .where(*base_filters)
                .group_by(ExchangeAuditLogModel.seller_agent_id)
                .order_by(desc("volume"))
                .limit(10)
            )
            top_sellers = [
                {"agent_id": row[0], "volume": str(row[1])} for row in session.execute(seller_stmt)
            ]

            return {
                "total_volume": str(total_volume),
                "tx_count": tx_count,
                "top_buyers": top_buyers,
                "top_sellers": top_sellers,
            }
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Integrity verification
    # ------------------------------------------------------------------

    def verify_integrity(self, record_id: str) -> bool:
        """Recompute a record's hash and compare to stored value.

        Note: SQLite strips timezone info from stored datetimes, so we
        normalize to UTC before recomputing the hash.
        """
        row = self.get_transaction(record_id)
        if row is None:
            return False
        return self.verify_integrity_from_row(row)

    def verify_integrity_from_row(self, row: ExchangeAuditLogModel) -> bool:
        """Verify integrity using an already-fetched row (avoids double-fetch).

        Accepts a row that may already be detached from a session.
        All needed attributes must be eagerly loaded before calling.
        """
        # Normalize: if tzinfo was lost (SQLite), assume UTC
        created = row.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)

        expected = compute_record_hash(
            protocol=row.protocol,
            buyer_agent_id=row.buyer_agent_id,
            seller_agent_id=row.seller_agent_id,
            amount=row.amount,
            currency=row.currency,
            status=row.status,
            application=row.application,
            zone_id=row.zone_id,
            trace_id=row.trace_id,
            transfer_id=row.transfer_id,
            created_at=created,
        )
        return expected == row.record_hash

    def compute_merkle_root(
        self,
        first_id: str,
        last_id: str,
    ) -> str:
        """Compute Merkle root over a range of records (by created_at order)."""
        session = self._session_factory()
        try:
            # Get boundary timestamps
            first_row = session.execute(
                select(ExchangeAuditLogModel).where(ExchangeAuditLogModel.id == first_id)
            ).scalar_one_or_none()
            last_row = session.execute(
                select(ExchangeAuditLogModel).where(ExchangeAuditLogModel.id == last_id)
            ).scalar_one_or_none()

            if first_row is None or last_row is None:
                return hashlib.sha256(b"").hexdigest()

            stmt = (
                select(ExchangeAuditLogModel.record_hash)
                .where(ExchangeAuditLogModel.created_at >= first_row.created_at)
                .where(ExchangeAuditLogModel.created_at <= last_row.created_at)
                .order_by(ExchangeAuditLogModel.created_at, ExchangeAuditLogModel.id)
            )
            hashes = [row[0] for row in session.execute(stmt)]
            return _build_merkle_root(hashes)
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Streaming export
    # ------------------------------------------------------------------

    def iter_transactions(
        self,
        *,
        filters: dict[str, Any] | None = None,
        batch_size: int = 500,
    ) -> list[ExchangeAuditLogModel]:
        """Fetch all matching transactions for export (in batches internally).

        For simplicity, returns a flat list. For truly large exports,
        callers should use cursor pagination directly.
        """
        all_rows: list[ExchangeAuditLogModel] = []
        cursor: str | None = None
        while True:
            rows, cursor = self.list_transactions_cursor(
                filters=filters, limit=batch_size, cursor=cursor
            )
            all_rows.extend(rows)
            if cursor is None:
                break
            # Safety: cap at 50k rows for export
            if len(all_rows) >= 50_000:
                break
        return all_rows

    def estimate_pages(
        self,
        *,
        filters: dict[str, Any] | None = None,
        page_size: int = 100,
    ) -> int:
        """Estimate total pages for a given filter set."""
        total = self.count_transactions(**(filters or {}))
        return max(1, math.ceil(total / page_size))
