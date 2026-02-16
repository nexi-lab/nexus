"""PostgreSQL implementation of PersistentViewStore (Issue #1265).

L3 cache layer — persists namespace views for instant agent reconnection.
Routes through RecordStoreABC (the RecordStore pillar) for engine access.

Storage Affinity: **RecordStore** — relational upsert via RecordStoreABC.engine.

Upsert semantics: DELETE + INSERT (portable across PostgreSQL and SQLite).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import text

from nexus.core.persistent_view_store import PersistentView
from nexus.storage.models._base import _generate_uuid

if TYPE_CHECKING:
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL Queries
# ---------------------------------------------------------------------------

_LOAD_VIEW = text("""
    SELECT subject_type, subject_id, zone_id, mount_paths_json,
           grants_hash, revision_bucket, created_at
    FROM persistent_namespace_views
    WHERE subject_type = :subject_type
      AND subject_id = :subject_id
      AND zone_id = :zone_id
""")

_DELETE_EXACT = text("""
    DELETE FROM persistent_namespace_views
    WHERE subject_type = :subject_type
      AND subject_id = :subject_id
      AND zone_id = :zone_id
""")

_INSERT_VIEW = text("""
    INSERT INTO persistent_namespace_views
        (id, subject_type, subject_id, zone_id, mount_paths_json,
         grants_hash, revision_bucket, created_at, updated_at)
    VALUES
        (:id, :subject_type, :subject_id, :zone_id, :mount_paths_json,
         :grants_hash, :revision_bucket, :created_at, :updated_at)
""")

_DELETE_SUBJECT = text("""
    DELETE FROM persistent_namespace_views
    WHERE subject_type = :subject_type
      AND subject_id = :subject_id
""")

_DELETE_ALL = text("""
    DELETE FROM persistent_namespace_views
""")


class PostgresPersistentViewStore:
    """PostgreSQL-backed persistent namespace view store.

    Implements PersistentViewStore protocol via structural subtyping.
    Routes through RecordStoreABC for engine access (Four Pillars compliance).
    """

    def __init__(self, record_store: RecordStoreABC) -> None:
        self._engine = record_store.engine

    def save_view(
        self,
        subject_type: str,
        subject_id: str,
        zone_id: str | None,
        mount_paths: list[str],
        grants_hash: str,
        revision_bucket: int,
    ) -> None:
        """Persist a namespace view (upsert via DELETE + INSERT)."""
        effective_zone = zone_id or "default"
        now = datetime.now(UTC)
        view_id = _generate_uuid()

        params = {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "zone_id": effective_zone,
        }

        with self._engine.begin() as conn:
            conn.execute(_DELETE_EXACT, params)
            conn.execute(
                _INSERT_VIEW,
                {
                    **params,
                    "id": view_id,
                    "mount_paths_json": json.dumps(mount_paths),
                    "grants_hash": grants_hash,
                    "revision_bucket": revision_bucket,
                    "created_at": now,
                    "updated_at": now,
                },
            )

    def load_view(
        self,
        subject_type: str,
        subject_id: str,
        zone_id: str | None,
    ) -> PersistentView | None:
        """Load a persisted namespace view."""
        effective_zone = zone_id or "default"

        with self._engine.connect() as conn:
            result = conn.execute(
                _LOAD_VIEW,
                {
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "zone_id": effective_zone,
                },
            )
            row = result.fetchone()

        if row is None:
            return None

        # Validate mount_paths_json deserialization (CRITICAL: data integrity boundary)
        raw = json.loads(row.mount_paths_json)
        if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
            logger.warning(
                "[L3] Corrupted mount_paths_json for %s:%s, treating as miss",
                row.subject_type,
                row.subject_id,
            )
            return None
        mount_paths: tuple[str, ...] = tuple(raw)

        return PersistentView(
            subject_type=row.subject_type,
            subject_id=row.subject_id,
            zone_id=row.zone_id if row.zone_id != "default" else None,
            mount_paths=mount_paths,
            grants_hash=row.grants_hash,
            revision_bucket=int(row.revision_bucket),
            created_at=row.created_at,
        )

    def delete_views(
        self,
        subject_type: str,
        subject_id: str,
    ) -> int:
        """Delete all persisted views for a subject (all zones)."""
        with self._engine.begin() as conn:
            result = conn.execute(
                _DELETE_SUBJECT,
                {
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                },
            )
            return result.rowcount or 0

    def delete_all_views(self) -> int:
        """Delete all persisted views across all subjects and zones."""
        with self._engine.begin() as conn:
            result = conn.execute(_DELETE_ALL)
            return result.rowcount or 0
