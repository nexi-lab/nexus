"""Aspect service — RecordStore implementation of AspectServiceProtocol (Issue #2929).

Implements the version-0 swap pattern with optimistic locking,
inline compaction, and MCL recording via operation_log.

Architecture:
    AspectService → EntityAspectModel (SQL side-store)
    AspectService → OperationLogger (MCL in operation_log, Key Decision #2)
    AspectService → AspectRegistry (schema validation)
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from nexus.contracts.aspects import AspectRegistry
from nexus.storage.models.aspect_store import EntityAspectModel

logger = logging.getLogger(__name__)


class AspectService:
    """RecordStore-backed aspect CRUD with version-0 pattern.

    Structurally satisfies ``AspectServiceProtocol``.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def _record_mcl(
        self,
        entity_urn: str,
        aspect_name: str,
        change_type: str,
        aspect_value: dict[str, Any] | None = None,
        zone_id: str | None = None,
        changed_by: str = "system",  # noqa: ARG002
    ) -> None:
        """Record an MCL event into operation_log (Key Decision #2).

        Writes aspect mutations as operation_log rows with MCL columns
        (entity_urn, aspect_name, change_type). This makes all aspect
        changes visible to OperationLogger.replay_changes().
        """
        from nexus.storage.operation_logger import OperationLogger

        op_type = "aspect_upsert" if change_type == "upsert" else "aspect_delete"
        OperationLogger(self._session).log_operation(
            operation_type=op_type,
            path=entity_urn,  # aspect mutations use URN as path
            zone_id=zone_id,
            status="success",
            metadata_snapshot=aspect_value,
            entity_urn=entity_urn,
            aspect_name=aspect_name,
            change_type=change_type,
        )

    def get_aspect(
        self,
        entity_urn: str,
        aspect_name: str,
    ) -> dict[str, Any] | None:
        """Get the current (version 0) aspect for an entity."""
        stmt = select(EntityAspectModel).where(
            EntityAspectModel.entity_urn == entity_urn,
            EntityAspectModel.aspect_name == aspect_name,
            EntityAspectModel.version == 0,
            EntityAspectModel.deleted_at.is_(None),
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        result: dict[str, Any] = json.loads(row.payload)
        return result

    def get_aspect_version(
        self,
        entity_urn: str,
        aspect_name: str,
        version: int,
    ) -> dict[str, Any] | None:
        """Get a specific version of an aspect."""
        stmt = select(EntityAspectModel).where(
            EntityAspectModel.entity_urn == entity_urn,
            EntityAspectModel.aspect_name == aspect_name,
            EntityAspectModel.version == version,
            EntityAspectModel.deleted_at.is_(None),
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        result: dict[str, Any] = json.loads(row.payload)
        return result

    def put_aspect(
        self,
        entity_urn: str,
        aspect_name: str,
        payload: dict[str, Any],
        *,
        created_by: str = "system",
        zone_id: str | None = None,
        record_mcl: bool = True,
    ) -> int:
        """Create or update an aspect using version-0 swap pattern.

        Args:
            record_mcl: If False, skip MCL recording (used during reindex replay
                to avoid self-amplification — replaying MCL rows should not
                generate new MCL rows).
        """
        registry = AspectRegistry.get()
        registry.validate_payload(aspect_name, payload)

        # Get current version 0 (with FOR UPDATE lock)
        stmt = (
            select(EntityAspectModel)
            .where(
                EntityAspectModel.entity_urn == entity_urn,
                EntityAspectModel.aspect_name == aspect_name,
                EntityAspectModel.version == 0,
                EntityAspectModel.deleted_at.is_(None),
            )
            .with_for_update()
        )
        current = self._session.execute(stmt).scalar_one_or_none()

        new_history_version = 0

        if current is not None:
            # Find next history version number
            max_version_result = self._session.execute(
                select(func.coalesce(func.max(EntityAspectModel.version), 0)).where(
                    EntityAspectModel.entity_urn == entity_urn,
                    EntityAspectModel.aspect_name == aspect_name,
                    EntityAspectModel.deleted_at.is_(None),
                )
            ).scalar()
            new_history_version = (
                (int(max_version_result) + 1) if max_version_result is not None else 1
            )

            # Copy current to history version
            history = EntityAspectModel(
                entity_urn=entity_urn,
                aspect_name=aspect_name,
                version=new_history_version,
                payload=current.payload,
                created_by=current.created_by,
                created_at=current.created_at,
                lock_version=0,
            )
            self._session.add(history)

            # Update version 0 in place
            current.payload = json.dumps(payload, default=str)
            current.created_by = created_by
            current.created_at = datetime.now(UTC)
            current.lock_version += 1
        else:
            # First version — insert version 0.
            # Check for soft-deleted rows that would conflict on the unique index
            # (SQLite partial index or app-level conflict resolution).
            deleted_row = self._session.execute(
                select(EntityAspectModel)
                .where(
                    EntityAspectModel.entity_urn == entity_urn,
                    EntityAspectModel.aspect_name == aspect_name,
                    EntityAspectModel.version == 0,
                    EntityAspectModel.deleted_at.is_not(None),
                )
                .limit(1)
            ).scalar_one_or_none()

            if deleted_row is not None:
                # Reuse the soft-deleted row: clear deleted_at, update payload
                deleted_row.payload = json.dumps(payload, default=str)
                deleted_row.created_by = created_by
                deleted_row.created_at = datetime.now(UTC)
                deleted_row.deleted_at = None
                deleted_row.lock_version = 0
            else:
                new_aspect = EntityAspectModel(
                    entity_urn=entity_urn,
                    aspect_name=aspect_name,
                    version=0,
                    payload=json.dumps(payload, default=str),
                    created_by=created_by,
                    lock_version=0,
                )
                self._session.add(new_aspect)

        # Inline compaction: delete old versions beyond max_versions
        max_versions = registry.max_versions_for(aspect_name)
        self._compact_versions(entity_urn, aspect_name, max_versions)

        # Record MCL (skipped during reindex replay to avoid self-amplification)
        if record_mcl:
            self._record_mcl(
                entity_urn=entity_urn,
                aspect_name=aspect_name,
                change_type="upsert",
                aspect_value=payload,
                zone_id=zone_id,
                changed_by=created_by,
            )

        self._session.flush()
        return new_history_version

    def _compact_versions(
        self,
        entity_urn: str,
        aspect_name: str,
        max_versions: int,
    ) -> None:
        """Delete versions older than max_versions (inline compaction)."""
        # Get all versions > 0, ordered by version desc
        stmt = (
            select(EntityAspectModel.aspect_id, EntityAspectModel.version)
            .where(
                EntityAspectModel.entity_urn == entity_urn,
                EntityAspectModel.aspect_name == aspect_name,
                EntityAspectModel.version > 0,
                EntityAspectModel.deleted_at.is_(None),
            )
            .order_by(EntityAspectModel.version.desc())
        )
        versions = list(self._session.execute(stmt).all())

        if len(versions) > max_versions:
            # Delete the excess (oldest) versions
            to_delete = [row[0] for row in versions[max_versions:]]
            if to_delete:
                self._session.execute(
                    update(EntityAspectModel)
                    .where(EntityAspectModel.aspect_id.in_(to_delete))
                    .values(deleted_at=datetime.now(UTC))
                )

    def delete_aspect(
        self,
        entity_urn: str,
        aspect_name: str,
        *,
        zone_id: str | None = None,
        record_mcl: bool = True,
    ) -> bool:
        """Soft-delete an aspect (all versions).

        Args:
            record_mcl: If False, skip MCL recording (used during reindex replay).
        """
        # Get current value for MCL
        current = self.get_aspect(entity_urn, aspect_name)
        if current is None:
            return False

        now = datetime.now(UTC)
        self._session.execute(
            update(EntityAspectModel)
            .where(
                EntityAspectModel.entity_urn == entity_urn,
                EntityAspectModel.aspect_name == aspect_name,
                EntityAspectModel.deleted_at.is_(None),
            )
            .values(deleted_at=now)
        )

        if record_mcl:
            self._record_mcl(
                entity_urn=entity_urn,
                aspect_name=aspect_name,
                change_type="delete",
                zone_id=zone_id,
            )

        self._session.flush()
        return True

    def list_aspects(
        self,
        entity_urn: str,
    ) -> list[str]:
        """List all current aspect names for an entity."""
        stmt = (
            select(EntityAspectModel.aspect_name)
            .where(
                EntityAspectModel.entity_urn == entity_urn,
                EntityAspectModel.version == 0,
                EntityAspectModel.deleted_at.is_(None),
            )
            .distinct()
        )
        return [row[0] for row in self._session.execute(stmt).all()]

    def get_aspects_batch(
        self,
        entity_urns: list[str],
        aspect_name: str,
    ) -> dict[str, dict[str, Any]]:
        """Batch-load current aspects for multiple entities (N+1 prevention)."""
        if not entity_urns:
            return {}

        stmt = select(EntityAspectModel).where(
            EntityAspectModel.entity_urn.in_(entity_urns),
            EntityAspectModel.aspect_name == aspect_name,
            EntityAspectModel.version == 0,
            EntityAspectModel.deleted_at.is_(None),
        )
        rows = self._session.execute(stmt).scalars().all()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            result[row.entity_urn] = json.loads(row.payload)
        return result

    def find_entities_with_aspect(
        self,
        aspect_name: str,
    ) -> dict[str, dict[str, Any]]:
        """Find all entities that have a given aspect (current version).

        Scans entity_aspects WHERE aspect_name=? AND version=0 AND deleted_at IS NULL.
        Returns dict mapping entity_urn → payload.
        """
        stmt = select(EntityAspectModel).where(
            EntityAspectModel.aspect_name == aspect_name,
            EntityAspectModel.version == 0,
            EntityAspectModel.deleted_at.is_(None),
        )
        rows = self._session.execute(stmt).scalars().all()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            result[row.entity_urn] = json.loads(row.payload)
        return result

    def soft_delete_entity_aspects(
        self,
        entity_urn: str,
    ) -> int:
        """Soft-delete all aspects for an entity (cascade)."""
        now = datetime.now(UTC)
        result = self._session.execute(
            update(EntityAspectModel)
            .where(
                EntityAspectModel.entity_urn == entity_urn,
                EntityAspectModel.deleted_at.is_(None),
            )
            .values(deleted_at=now)
        )
        count: int = getattr(result, "rowcount", 0) or 0
        if count > 0:
            self._session.flush()
        return count

    def get_aspect_history(
        self,
        entity_urn: str,
        aspect_name: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Get version history for an aspect, newest first."""
        stmt = (
            select(EntityAspectModel)
            .where(
                EntityAspectModel.entity_urn == entity_urn,
                EntityAspectModel.aspect_name == aspect_name,
                EntityAspectModel.deleted_at.is_(None),
            )
            .order_by(EntityAspectModel.version.desc())
            .limit(limit)
        )
        rows = self._session.execute(stmt).scalars().all()
        return [
            {
                "version": row.version,
                "payload": json.loads(row.payload),
                "created_by": row.created_by,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
