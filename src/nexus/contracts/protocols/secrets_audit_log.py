"""Secrets audit log service protocol.

Defines the contract for secrets/credential audit logging — recording
every OAuth credential and token lifecycle event with cryptographic
integrity (SHA-256 self-hash).

Storage Affinity: **RecordStore** (append-only audit log records with
                  timestamps, zone scoping, and integrity hashes).

References:
    - docs/architecture/data-storage-matrix.md  (Four Pillars)
    - Issue #997: Secrets audit trail
"""

from typing import Any, Protocol, runtime_checkable

from nexus.contracts.constants import ROOT_ZONE_ID


@runtime_checkable
class SecretsAuditLogProtocol(Protocol):
    """Service contract for secrets/credential audit logging.

    Mirrors ``storage/secrets_audit_logger.SecretsAuditLogger``.

    Provides append-only, cryptographically immutable event records
    with cursor-based pagination and integrity verification.
    """

    def log_event(
        self,
        *,
        event_type: str,
        actor_id: str,
        provider: str | None = None,
        credential_id: str | None = None,
        token_family_id: str | None = None,
        zone_id: str = ROOT_ZONE_ID,
        ip_address: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> str:
        """Record a secrets audit event (append-only).

        Returns:
            The record ID (UUID string).
        """
        ...

    def get_event(self, record_id: str) -> Any | None:
        """Get a single audit event by ID."""
        ...

    def list_events_cursor(
        self,
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[Any], str | None]:
        """Query events with cursor-based pagination."""
        ...

    def count_events(self, **filters: Any) -> int:
        """Count matching events."""
        ...

    def iter_events(
        self,
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 10_000,
    ) -> list[Any]:
        """Fetch matching events for export."""
        ...

    def verify_integrity(self, record_id: str) -> bool:
        """Verify a record's hash matches its data (tamper detection)."""
        ...

    def verify_integrity_from_row(self, row: Any) -> bool:
        """Verify integrity from an already-loaded row."""
        ...
