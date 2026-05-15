"""Lineage service — records and queries agent lineage (Issue #3417).

Business logic for:
    - Recording lineage (aspect + reverse index, atomic per file)
    - Querying upstream lineage for a file
    - Querying downstream dependents (impact analysis)
    - Staleness detection (comparing stored vs current upstream versions)

Architecture:
    LineageService → AspectService (lineage aspect CRUD)
    LineageService → LineageReverseIndexModel (reverse lookup index)
    LineageService → OperationLogger (MCL via AspectService)
"""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from nexus.contracts.aspects import LineageAspect
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.storage.aspect_service import AspectService
from nexus.storage.models.lineage_reverse_index import LineageReverseIndexModel

logger = logging.getLogger(__name__)

LINEAGE_ASPECT_NAME = "lineage"


class LineageService:
    """Records and queries agent lineage relationships.

    All operations use a single SQLAlchemy session. The caller is
    responsible for commit/rollback/close.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._aspect_service = AspectService(session)

    def record_lineage(
        self,
        entity_urn: str,
        lineage: LineageAspect,
        *,
        zone_id: str | None = None,
        downstream_path: str | None = None,
        record_mcl: bool = True,
    ) -> None:
        """Record lineage for an output file (aspect + reverse index).

        Uses a savepoint so that failure rolls back both the aspect and
        reverse index atomically, without affecting the outer transaction.

        Args:
            entity_urn: URN of the output file.
            lineage: LineageAspect with upstream entries.
            zone_id: Zone for zone isolation.
            downstream_path: Virtual path of the output (for display in reverse index).
            record_mcl: If False, skip MCL recording (for replay).
        """
        if not lineage.upstream:
            return  # No reads → no lineage to record

        with self._session.begin_nested():
            # 1. Write lineage aspect (source of truth)
            payload = lineage.to_dict()
            self._aspect_service.put_aspect(
                entity_urn=entity_urn,
                aspect_name=LINEAGE_ASPECT_NAME,
                payload=payload,
                created_by=lineage.agent_id or "system",
                zone_id=zone_id,
                record_mcl=record_mcl,
            )

            # 2. Upsert reverse index: delete old entries, insert new
            self._session.execute(
                delete(LineageReverseIndexModel).where(
                    LineageReverseIndexModel.downstream_urn == entity_urn,
                )
            )

            # 3. Batch insert new reverse index entries
            now = datetime.now(UTC)
            entries = [
                LineageReverseIndexModel(
                    upstream_path=upstream["path"],
                    downstream_urn=entity_urn,
                    zone_id=zone_id or ROOT_ZONE_ID,
                    upstream_version=upstream.get("version", 0),
                    upstream_content_id=upstream.get("content_id", ""),
                    access_type=upstream.get("access_type", "content"),
                    agent_id=lineage.agent_id or "",
                    downstream_path=downstream_path,
                    created_at=now,
                )
                for upstream in lineage.upstream
            ]
            self._session.add_all(entries)

    def get_lineage(
        self,
        entity_urn: str,
    ) -> dict[str, Any] | None:
        """Get the current lineage aspect for an entity.

        Args:
            entity_urn: URN of the file to query.

        Returns:
            Lineage payload dict, or None if no lineage recorded.
        """
        return self._aspect_service.get_aspect(entity_urn, LINEAGE_ASPECT_NAME)

    def find_downstream(
        self,
        upstream_path: str,
        *,
        zone_id: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Find all downstream entities that depend on an upstream path.

        Uses the reverse index for O(1) lookup.

        Args:
            upstream_path: Path of the upstream file.
            zone_id: Optional zone filter.
            limit: Max results.

        Returns:
            List of dicts with downstream_urn, downstream_path, upstream_version,
            upstream_content_id, agent_id.
        """
        stmt = select(LineageReverseIndexModel).where(
            LineageReverseIndexModel.upstream_path == upstream_path,
        )
        if zone_id:
            stmt = stmt.where(LineageReverseIndexModel.zone_id == zone_id)
        stmt = stmt.limit(limit)

        rows = self._session.execute(stmt).scalars().all()
        return [
            {
                "downstream_urn": row.downstream_urn,
                "downstream_path": row.downstream_path,
                "upstream_version": row.upstream_version,
                "upstream_content_id": row.upstream_content_id,
                "access_type": row.access_type,
                "agent_id": row.agent_id,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]

    def check_staleness(
        self,
        upstream_path: str,
        current_version: int,
        current_content_id: str,
        *,
        zone_id: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Find downstream entities that are stale because upstream changed.

        A downstream is stale if its recorded upstream_version != current_version
        OR its recorded upstream_content_id != current_content_id.

        Uses denormalized version/content_id in the reverse index for a single query.

        Args:
            upstream_path: Path of the changed upstream file.
            current_version: Current version of the upstream file.
            current_content_id: Current content hash of the upstream file.
            zone_id: Optional zone filter.
            limit: Max results.

        Returns:
            List of stale downstream dicts with recorded vs current version info.
        """
        stmt = select(LineageReverseIndexModel).where(
            LineageReverseIndexModel.upstream_path == upstream_path,
        )
        if zone_id:
            stmt = stmt.where(LineageReverseIndexModel.zone_id == zone_id)
        stmt = stmt.limit(limit)

        rows = self._session.execute(stmt).scalars().all()

        stale = []
        for row in rows:
            # Not stale if both version AND content_id match
            if (
                row.upstream_version == current_version
                and row.upstream_content_id == current_content_id
            ):
                continue

            # Stale: version or content_id differs
            stale.append(
                {
                    "downstream_urn": row.downstream_urn,
                    "downstream_path": row.downstream_path,
                    "recorded_version": row.upstream_version,
                    "recorded_content_id": row.upstream_content_id,
                    "current_version": current_version,
                    "current_content_id": current_content_id,
                    "agent_id": row.agent_id,
                }
            )
        return stale

    def delete_lineage(
        self,
        entity_urn: str,
        *,
        zone_id: str | None = None,
    ) -> bool:
        """Delete lineage for an entity (aspect + reverse index).

        Args:
            entity_urn: URN of the entity.
            zone_id: Zone for MCL recording.

        Returns:
            True if lineage existed and was deleted.
        """
        with self._session.begin_nested():
            deleted = self._aspect_service.delete_aspect(
                entity_urn, LINEAGE_ASPECT_NAME, zone_id=zone_id
            )
            self._session.execute(
                delete(LineageReverseIndexModel).where(
                    LineageReverseIndexModel.downstream_urn == entity_urn,
                )
            )
        return deleted

    def compact_orphaned_entries(
        self,
        valid_downstream_urns: set[str] | None = None,
        *,
        max_age_days: int = 90,
    ) -> int:
        """Remove reverse index entries for entities that no longer exist.

        Args:
            valid_downstream_urns: Set of URNs that still exist. If None,
                falls back to age-based cleanup.
            max_age_days: Remove entries older than this (if no URN set provided).

        Returns:
            Number of entries removed.
        """
        if valid_downstream_urns is not None:
            # Delete entries whose downstream_urn is not in the valid set
            all_entries = self._session.execute(
                select(LineageReverseIndexModel.entry_id, LineageReverseIndexModel.downstream_urn)
            ).all()
            orphaned_ids = [row[0] for row in all_entries if row[1] not in valid_downstream_urns]
            if orphaned_ids:
                self._session.execute(
                    delete(LineageReverseIndexModel).where(
                        LineageReverseIndexModel.entry_id.in_(orphaned_ids)
                    )
                )
            return len(orphaned_ids)

        # Age-based cleanup
        cutoff = datetime.now(UTC).replace(
            day=max(1, datetime.now(UTC).day),
        )
        # Simple age-based: delete entries older than max_age_days
        from datetime import timedelta

        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        result = self._session.execute(
            delete(LineageReverseIndexModel).where(
                LineageReverseIndexModel.created_at < cutoff,
            )
        )
        return getattr(result, "rowcount", 0) or 0
