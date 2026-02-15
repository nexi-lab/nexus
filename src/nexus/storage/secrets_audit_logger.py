"""Secrets audit logger — immutable audit trail for credential operations.

Issue #997: Records every OAuth credential and token lifecycle event
with cryptographic integrity (SHA-256 self-hash).  Follows the
ExchangeAuditLogger pattern with append-only immutability guards.

Write performance: callers should use ``asyncio.create_task()`` to
fire-and-forget audit writes so they never block the hot path.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from nexus.storage.models.secrets_audit_log import SecretsAuditLogModel
from nexus.storage.query_mixin import AppendOnlyQueryMixin

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Immutability guards
# ---------------------------------------------------------------------------


@event.listens_for(SecretsAuditLogModel, "before_update")
def _reject_update(mapper: Any, connection: Any, target: Any) -> None:  # noqa: ARG001
    """Prevent any UPDATE on secrets audit log records at the ORM level."""
    raise RuntimeError("Secrets audit log records are immutable: UPDATE not allowed")


@event.listens_for(SecretsAuditLogModel, "before_delete")
def _reject_delete(mapper: Any, connection: Any, target: Any) -> None:  # noqa: ARG001
    """Prevent any DELETE on secrets audit log records at the ORM level."""
    raise RuntimeError("Secrets audit log records are immutable: DELETE not allowed")


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------


def compute_record_hash(
    *,
    event_type: str,
    actor_id: str,
    provider: str | None,
    credential_id: str | None,
    token_family_id: str | None,
    zone_id: str,
    ip_address: str | None,
    created_at: datetime,
) -> str:
    """Compute SHA-256 hash over canonical field representation.

    Covers every business-relevant field so that any post-hoc
    tampering is detectable via ``verify_integrity``.
    """
    canonical = "|".join(
        [
            event_type,
            actor_id,
            provider or "",
            credential_id or "",
            token_family_id or "",
            zone_id,
            ip_address or "",
            created_at.isoformat(),
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_metadata_hash(details: dict[str, Any] | None) -> str | None:
    """SHA-256 of deterministically-serialized details, or None."""
    if not details:
        return None
    serialized = json.dumps(details, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# SecretsAuditLogger
# ---------------------------------------------------------------------------


class SecretsAuditLogger:
    """Append-only secrets audit logger with query capabilities.

    Args:
        session_factory: Callable that returns a fresh SQLAlchemy Session.
    """

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory
        self._query = AppendOnlyQueryMixin(
            model_class=SecretsAuditLogModel,
            id_column_name="id",
            created_column_name="created_at",
        )

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def log_event(
        self,
        *,
        event_type: str,
        actor_id: str,
        provider: str | None = None,
        credential_id: str | None = None,
        token_family_id: str | None = None,
        zone_id: str = "default",
        ip_address: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> str:
        """Record a secrets audit event (append-only).

        Returns:
            The record ID (UUID string).
        """
        now = datetime.now(UTC)
        record_hash = compute_record_hash(
            event_type=event_type,
            actor_id=actor_id,
            provider=provider,
            credential_id=credential_id,
            token_family_id=token_family_id,
            zone_id=zone_id,
            ip_address=ip_address,
            created_at=now,
        )
        metadata_h = compute_metadata_hash(details)
        details_json = json.dumps(details, sort_keys=True, default=str) if details else None

        row = SecretsAuditLogModel(
            record_hash=record_hash,
            created_at=now,
            event_type=event_type,
            actor_id=actor_id,
            provider=provider,
            credential_id=credential_id,
            token_family_id=token_family_id,
            zone_id=zone_id,
            ip_address=ip_address,
            details=details_json,
            metadata_hash=metadata_h,
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

    def get_event(self, record_id: str) -> SecretsAuditLogModel | None:
        """Get a single audit event by ID."""
        session = self._session_factory()
        try:
            stmt = select(SecretsAuditLogModel).where(SecretsAuditLogModel.id == record_id)
            return session.execute(stmt).scalar_one_or_none()
        finally:
            session.close()

    def list_events_cursor(
        self,
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[SecretsAuditLogModel], str | None]:
        """Query events with cursor-based pagination."""
        session = self._session_factory()
        try:
            return self._query.list_cursor(session, filters=filters, limit=limit, cursor=cursor)
        finally:
            session.close()

    def count_events(self, **filters: Any) -> int:
        """Count matching events."""
        session = self._session_factory()
        try:
            return self._query.count(session, filters=filters)
        finally:
            session.close()

    def iter_events(
        self,
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 10_000,
    ) -> list[SecretsAuditLogModel]:
        """Fetch matching events (for export), capped at ``limit``."""
        session = self._session_factory()
        try:
            stmt = (
                select(SecretsAuditLogModel)
                .order_by(
                    SecretsAuditLogModel.created_at.desc(),
                    SecretsAuditLogModel.id.desc(),
                )
                .limit(limit)
            )
            if filters:
                stmt = self._query.apply_filters(stmt, filters=filters)
            return list(session.execute(stmt).scalars())
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Integrity verification
    # ------------------------------------------------------------------

    def verify_integrity(self, record_id: str) -> bool:
        """Verify a record's hash matches its data (tamper detection).

        Returns True if the recomputed hash matches the stored hash.
        """
        row = self.get_event(record_id)
        if row is None:
            return False
        return self.verify_integrity_from_row(row)

    def verify_integrity_from_row(self, row: SecretsAuditLogModel) -> bool:
        """Verify integrity from an already-loaded row."""
        # SQLite drops timezone info — normalize to UTC for hash comparison
        created_at = row.created_at
        if created_at is not None and created_at.tzinfo is None:
            from datetime import UTC

            created_at = created_at.replace(tzinfo=UTC)

        expected = compute_record_hash(
            event_type=row.event_type,
            actor_id=row.actor_id,
            provider=row.provider,
            credential_id=row.credential_id,
            token_family_id=row.token_family_id,
            zone_id=row.zone_id,
            ip_address=row.ip_address,
            created_at=created_at,
        )
        return hmac.compare_digest(row.record_hash, expected)
