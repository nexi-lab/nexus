"""Exchange audit log service protocol.

Defines the contract for exchange transaction audit logging — recording
every exchange with cryptographic integrity (SHA-256 self-hash) and
providing query/export capabilities.

Storage Affinity: **RecordStore** (append-only audit log records with
                  timestamps, zone scoping, and integrity hashes).

References:
    - docs/architecture/data-storage-matrix.md  (Four Pillars)
    - Issue #1360: Exchange audit trail
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from nexus.contracts.constants import ROOT_ZONE_ID


@runtime_checkable
class ExchangeAuditLogProtocol(Protocol):
    """Service contract for exchange transaction audit logging.

    Mirrors ``storage/exchange_audit_logger.ExchangeAuditLogger``.

    Provides append-only, cryptographically immutable transaction records
    with cursor-based pagination, aggregations, and integrity verification.
    """

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
        zone_id: str = ROOT_ZONE_ID,
        trace_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        transfer_id: str | None = None,
    ) -> str:
        """Record an exchange transaction (append-only).

        Returns:
            The record ID (UUID string).
        """
        ...

    def get_transaction(self, record_id: str) -> Any | None:
        """Get a single transaction by ID."""
        ...

    def list_transactions_cursor(
        self,
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[Any], str | None]:
        """Query transactions with cursor-based pagination."""
        ...

    def count_transactions(self, **filters: Any) -> int:
        """Count matching transactions."""
        ...

    def get_aggregations(
        self,
        *,
        zone_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, Any]:
        """Compute aggregations: total_volume, tx_count, top counterparties."""
        ...

    def verify_integrity(self, record_id: str) -> bool:
        """Recompute a record's hash and compare to stored value."""
        ...

    def verify_integrity_from_row(self, row: Any) -> bool:
        """Verify integrity using an already-fetched row."""
        ...

    def compute_merkle_root(self, first_id: str, last_id: str) -> str:
        """Compute Merkle root over a range of records."""
        ...

    def iter_transactions(
        self,
        *,
        filters: dict[str, Any] | None = None,
        batch_size: int = 500,
    ) -> list[Any]:
        """Fetch all matching transactions for export."""
        ...

    def estimate_pages(
        self,
        *,
        filters: dict[str, Any] | None = None,
        page_size: int = 100,
    ) -> int:
        """Estimate total pages for a given filter set."""
        ...
