"""Tuple Writer — handles write/delete operations for ReBAC tuples.

Extracts the core tuple write/delete logic from ``ReBACManager`` into
a focused module.  The manager retains thin facade methods that call
``TupleWriter`` for the SQL layer, then orchestrate cache invalidation
across Leopard, Tiger, L1, boundary, and directory subsystems.

Related: Issue #2179 (decomposition), Issue #773 (zone-id tracking)
"""

import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from nexus.bricks.rebac.domain import Entity
from nexus.bricks.rebac.tuples.repository import TupleRepository
from nexus.bricks.rebac.utils.changelog import insert_changelog_entry
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.lib.zone import normalize_zone_id

logger = logging.getLogger(__name__)


class TupleWriter:
    """Handles write/delete SQL operations for ReBAC tuples.

    Constructor dependencies mirror the thin helpers formerly on ReBACManager.
    Cache invalidation is NOT handled here — the manager's facade methods
    are responsible for that orchestration.
    """

    def __init__(
        self,
        *,
        connection_factory: Callable[..., Any],
        create_cursor: Callable[[Any], Any],
        fix_sql: Callable[[str], str],
        is_postgresql: bool,
        repo: TupleRepository,
        zone_manager: Any | None,
        ensure_namespaces_cb: Callable[[], None],
        validate_cross_zone_cb: Callable[[str | None, str | None, str | None], None],
        would_create_cycle_cb: Callable[..., bool],
        increment_zone_revision_cb: Callable[[str | None, Any], int],
        invalidate_cache_cb: Callable[..., None],
        get_tuple_version: Callable[[], int],
        set_tuple_version: Callable[[int], None],
    ) -> None:
        self._connection = connection_factory
        self._create_cursor = create_cursor
        self._fix_sql = fix_sql
        self._is_postgresql = is_postgresql
        self._repo = repo
        self._zone_manager = zone_manager
        self._ensure_namespaces_initialized = ensure_namespaces_cb
        self._validate_cross_zone = validate_cross_zone_cb
        self._would_create_cycle_with_conn = would_create_cycle_cb
        self._increment_zone_revision = increment_zone_revision_cb
        self._invalidate_cache_for_tuple = invalidate_cache_cb
        self._get_tuple_version = get_tuple_version
        self._set_tuple_version = set_tuple_version

    # ------------------------------------------------------------------
    # Zone-aware single write
    # ------------------------------------------------------------------

    def write_tuple_zone_aware(
        self,
        subject: tuple[str, str] | tuple[str, str, str],
        relation: str,
        object: tuple[str, str],
        enforce_zone_isolation: bool,
        expires_at: datetime | None = None,
        conditions: dict[str, Any] | None = None,
        zone_id: str | None = None,
        subject_zone_id: str | None = None,
        object_zone_id: str | None = None,
    ) -> str:
        """Insert a relationship tuple with zone isolation.

        If zone isolation is disabled, delegates to :meth:`write_base`.
        """
        self._ensure_namespaces_initialized()

        if not enforce_zone_isolation:
            return self.write_base(
                subject=subject,
                relation=relation,
                object=object,
                expires_at=expires_at,
                conditions=conditions,
                zone_id=zone_id,
                subject_zone_id=subject_zone_id,
                object_zone_id=object_zone_id,
            )

        # Delegate zone validation to ZoneManager
        if self._zone_manager is None:
            msg = "ZoneManager required when enforce_zone_isolation is True"
            raise RuntimeError(msg)
        zone_id, subject_zone_id, object_zone_id, _is_cross_zone = (
            self._zone_manager.validate_write_zones(
                zone_id, subject_zone_id, object_zone_id, relation
            )
        )

        subject_entity, subject_relation = _parse_subject(subject)
        tuple_id = str(uuid.uuid4())
        object_entity = Entity(object[0], object[1])

        with self._connection() as conn:
            if relation == "parent" and self._would_create_cycle_with_conn(
                conn, subject_entity, object_entity, zone_id
            ):
                raise ValueError(
                    f"Cycle detected: Creating parent relation from "
                    f"{subject_entity.entity_type}:{subject_entity.entity_id} to "
                    f"{object_entity.entity_type}:{object_entity.entity_id} would create a cycle"
                )

            cursor = self._create_cursor(conn)

            existing_id = self._check_existing_tuple(
                cursor, subject_entity, subject_relation, relation, object_entity, zone_id
            )
            if existing_id is not None:
                return existing_id

            cursor.execute(
                self._fix_sql(
                    """
                    INSERT INTO rebac_tuples (
                        tuple_id, zone_id, subject_type, subject_id, subject_relation,
                        subject_zone_id, relation, object_type, object_id, object_zone_id,
                        created_at, expires_at, conditions
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    tuple_id,
                    zone_id,
                    subject_entity.entity_type,
                    subject_entity.entity_id,
                    subject_relation,
                    subject_zone_id,
                    relation,
                    object_entity.entity_type,
                    object_entity.entity_id,
                    object_zone_id,
                    datetime.now(UTC).isoformat(),
                    expires_at.isoformat() if expires_at else None,
                    json.dumps(conditions) if conditions else None,
                ),
            )

            insert_changelog_entry(
                cursor,
                self._fix_sql,
                change_type="INSERT",
                tuple_id=tuple_id,
                subject_type=subject_entity.entity_type,
                subject_id=subject_entity.entity_id,
                relation=relation,
                object_type=object_entity.entity_type,
                object_id=object_entity.entity_id,
                zone_id=zone_id,
            )

            self._increment_zone_revision(zone_id, conn)
            conn.commit()
            self._set_tuple_version(self._get_tuple_version() + 1)

            self._invalidate_cache_for_tuple(
                subject_entity,
                relation,
                object_entity,
                zone_id,
                subject_relation,
                expires_at,
                conn=conn,
            )

            if subject_zone_id != zone_id:
                self._invalidate_cache_for_tuple(
                    subject_entity,
                    relation,
                    object_entity,
                    subject_zone_id,
                    subject_relation,
                    expires_at,
                    conn=conn,
                )

        return tuple_id

    # ------------------------------------------------------------------
    # Base write (no zone wrapping)
    # ------------------------------------------------------------------

    def write_base(
        self,
        subject: tuple[str, str] | tuple[str, str, str],
        relation: str,
        object: tuple[str, str],
        expires_at: datetime | None = None,
        conditions: dict[str, Any] | None = None,
        zone_id: str | None = None,
        subject_zone_id: str | None = None,
        object_zone_id: str | None = None,
    ) -> str:
        """Base tuple write — no zone-aware wrapping.

        Used when ``enforce_zone_isolation`` is False.
        """
        self._ensure_namespaces_initialized()

        tuple_id = str(uuid.uuid4())
        subject_entity, subject_relation = _parse_subject(subject)
        object_entity = Entity(object[0], object[1])

        if zone_id is None:
            zone_id = ROOT_ZONE_ID
        if subject_zone_id is None:
            subject_zone_id = zone_id
        if object_zone_id is None:
            object_zone_id = zone_id

        self._validate_cross_zone(zone_id, subject_zone_id, object_zone_id)

        with self._connection() as conn:
            if relation == "parent" and self._would_create_cycle_with_conn(
                conn, subject_entity, object_entity, zone_id
            ):
                raise ValueError(
                    f"Cycle detected: Creating parent relation from "
                    f"{subject_entity.entity_type}:{subject_entity.entity_id} to "
                    f"{object_entity.entity_type}:{object_entity.entity_id} would create a cycle"
                )
            cursor = self._create_cursor(conn)

            existing_id = self._check_existing_tuple(
                cursor, subject_entity, subject_relation, relation, object_entity, zone_id
            )
            if existing_id is not None:
                return existing_id

            cursor.execute(
                self._fix_sql(
                    """
                    INSERT INTO rebac_tuples (
                        tuple_id, subject_type, subject_id, subject_relation, relation,
                        object_type, object_id, created_at, expires_at, conditions,
                        zone_id, subject_zone_id, object_zone_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    tuple_id,
                    subject_entity.entity_type,
                    subject_entity.entity_id,
                    subject_relation,
                    relation,
                    object_entity.entity_type,
                    object_entity.entity_id,
                    datetime.now(UTC).isoformat(),
                    expires_at.isoformat() if expires_at else None,
                    json.dumps(conditions) if conditions else None,
                    zone_id,
                    subject_zone_id,
                    object_zone_id,
                ),
            )

            insert_changelog_entry(
                cursor,
                self._fix_sql,
                change_type="INSERT",
                tuple_id=tuple_id,
                subject_type=subject_entity.entity_type,
                subject_id=subject_entity.entity_id,
                relation=relation,
                object_type=object_entity.entity_type,
                object_id=object_entity.entity_id,
                zone_id=zone_id or ROOT_ZONE_ID,
            )

            self._increment_zone_revision(zone_id, conn)
            conn.commit()
            self._set_tuple_version(self._get_tuple_version() + 1)

            self._invalidate_cache_for_tuple(
                subject_entity,
                relation,
                object_entity,
                zone_id,
                subject_relation,
                expires_at,
                conn=conn,
            )

            if subject_zone_id is not None and subject_zone_id != zone_id:
                self._invalidate_cache_for_tuple(
                    subject_entity,
                    relation,
                    object_entity,
                    subject_zone_id,
                    subject_relation,
                    expires_at,
                    conn=conn,
                )

        return tuple_id

    # ------------------------------------------------------------------
    # Batch write
    # ------------------------------------------------------------------

    def write_batch(
        self,
        tuples: list[dict[str, Any]],
        l1_cache: Any | None = None,
    ) -> int:
        """Create multiple relationship tuples in a single transaction.

        Returns the number of tuples created (excluding duplicates).
        """
        if not tuples:
            return 0

        self._ensure_namespaces_initialized()

        created_count = 0
        now = datetime.now(UTC).isoformat()

        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            try:
                # Step 1: Parse and validate all tuples
                parsed_tuples: list[dict[str, Any]] = []
                for t in tuples:
                    subject = t["subject"]
                    relation = t["relation"]
                    obj = t["object"]
                    zone_id = t.get("zone_id")
                    expires_at = t.get("expires_at")
                    conditions = t.get("conditions")
                    subject_zone_id = t.get("subject_zone_id")
                    object_zone_id = t.get("object_zone_id")

                    subject_entity, subject_relation = _parse_subject(subject)
                    object_entity = Entity(obj[0], obj[1])

                    if zone_id is None:
                        zone_id = ROOT_ZONE_ID
                    if subject_zone_id is None:
                        subject_zone_id = zone_id
                    if object_zone_id is None:
                        object_zone_id = zone_id

                    self._validate_cross_zone(zone_id, subject_zone_id, object_zone_id)

                    if relation == "parent" and self._would_create_cycle_with_conn(
                        conn, subject_entity, object_entity, zone_id
                    ):
                        logger.warning(
                            "Skipping tuple creation - cycle detected: %s:%s -> %s:%s",
                            subject_entity.entity_type,
                            subject_entity.entity_id,
                            object_entity.entity_type,
                            object_entity.entity_id,
                        )
                        continue

                    parsed_tuples.append(
                        {
                            "tuple_id": str(uuid.uuid4()),
                            "subject_type": subject_entity.entity_type,
                            "subject_id": subject_entity.entity_id,
                            "subject_relation": subject_relation,
                            "subject_entity": subject_entity,
                            "relation": relation,
                            "object_type": obj[0],
                            "object_id": obj[1],
                            "object_entity": object_entity,
                            "zone_id": zone_id,
                            "expires_at": expires_at,
                            "conditions": conditions,
                            "subject_zone_id": subject_zone_id,
                            "object_zone_id": object_zone_id,
                        }
                    )

                if not parsed_tuples:
                    return 0

                # Step 2: Bulk check which tuples already exist
                existing_tuples = self._repo.bulk_check_tuples_exist(cursor, parsed_tuples)

                # Step 3: Filter out existing tuples
                tuples_to_create = []
                for pt in parsed_tuples:
                    key = (
                        (pt["subject_type"], pt["subject_id"], pt["subject_relation"]),
                        pt["relation"],
                        (pt["object_type"], pt["object_id"]),
                        pt["zone_id"],
                    )
                    if key not in existing_tuples:
                        tuples_to_create.append(pt)

                if not tuples_to_create:
                    return 0

                # Step 4: Bulk insert tuples
                tuple_insert_sql = self._fix_sql(
                    """
                    INSERT INTO rebac_tuples (
                        tuple_id, subject_type, subject_id, subject_relation, relation,
                        object_type, object_id, created_at, expires_at, conditions,
                        zone_id, subject_zone_id, object_zone_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                )

                tuple_data = [
                    (
                        pt["tuple_id"],
                        pt["subject_type"],
                        pt["subject_id"],
                        pt["subject_relation"],
                        pt["relation"],
                        pt["object_type"],
                        pt["object_id"],
                        now,
                        pt["expires_at"].isoformat() if pt["expires_at"] else None,
                        json.dumps(pt["conditions"]) if pt["conditions"] else None,
                        pt["zone_id"],
                        pt["subject_zone_id"],
                        pt["object_zone_id"],
                    )
                    for pt in tuples_to_create
                ]

                cursor.executemany(tuple_insert_sql, tuple_data)

                # Step 5: Bulk insert changelog entries
                changelog_insert_sql = self._fix_sql(
                    """
                    INSERT INTO rebac_changelog (
                        change_type, tuple_id, subject_type, subject_id,
                        relation, object_type, object_id, zone_id, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                )

                changelog_data = [
                    (
                        "INSERT",
                        pt["tuple_id"],
                        pt["subject_type"],
                        pt["subject_id"],
                        pt["relation"],
                        pt["object_type"],
                        pt["object_id"],
                        pt["zone_id"] or ROOT_ZONE_ID,
                        now,
                    )
                    for pt in tuples_to_create
                ]

                cursor.executemany(changelog_insert_sql, changelog_data)

                created_count = len(tuples_to_create)

                # Step 6: Batch cache invalidation
                invalidation_keys: set[tuple[str, str, str, str, str, str | None]] = set()
                for pt in tuples_to_create:
                    inv_key: tuple[str, str, str, str, str, str | None] = (
                        pt["subject_entity"].entity_type,
                        pt["subject_entity"].entity_id,
                        pt["relation"],
                        pt["object_entity"].entity_type,
                        pt["object_entity"].entity_id,
                        pt["zone_id"],
                    )
                    invalidation_keys.add(inv_key)

                    if pt["subject_zone_id"] and pt["subject_zone_id"] != pt["zone_id"]:
                        cross_inv_key: tuple[str, str, str, str, str, str | None] = (
                            pt["subject_entity"].entity_type,
                            pt["subject_entity"].entity_id,
                            pt["relation"],
                            pt["object_entity"].entity_type,
                            pt["object_entity"].entity_id,
                            pt["subject_zone_id"],
                        )
                        invalidation_keys.add(cross_inv_key)

                # L1 cache invalidation
                if l1_cache:
                    for inv_key in invalidation_keys:
                        subj_type, subj_id, _rel, obj_type, obj_id, tid = inv_key
                        l1_cache.invalidate_subject_object_pair(
                            subj_type, subj_id, obj_type, obj_id, tid
                        )

                # L2 cache: bulk delete affected entries
                if invalidation_keys:
                    delete_conditions = []
                    delete_params: list[str] = []
                    for inv_key in invalidation_keys:
                        subj_type, subj_id, _rel, obj_type, obj_id, tid = inv_key
                        delete_conditions.append(
                            "(zone_id = ? AND subject_type = ? AND subject_id = ? "
                            "AND object_type = ? AND object_id = ?)"
                        )
                        delete_params.extend(
                            [tid or ROOT_ZONE_ID, subj_type, subj_id, obj_type, obj_id]
                        )

                    chunk_size = 50
                    for i in range(0, len(delete_conditions), chunk_size):
                        chunk_conds = delete_conditions[i : i + chunk_size]
                        chunk_params = delete_params[i * 5 : (i + chunk_size) * 5]

                        if chunk_conds:
                            delete_sql = "DELETE FROM rebac_check_cache WHERE " + " OR ".join(
                                chunk_conds
                            )
                            cursor.execute(self._fix_sql(delete_sql), chunk_params)

                # Increment revision for all affected zones
                if created_count > 0:
                    affected_zones: set[str] = set()
                    for pt in parsed_tuples:
                        affected_zones.add(pt["zone_id"] or ROOT_ZONE_ID)
                        if pt["subject_zone_id"] and pt["subject_zone_id"] != pt["zone_id"]:
                            affected_zones.add(pt["subject_zone_id"])
                    for zone in affected_zones:
                        self._increment_zone_revision(zone, conn)

                conn.commit()
                if created_count > 0:
                    self._set_tuple_version(self._get_tuple_version() + 1)

            except Exception:  # rollback-then-reraise: ensures transaction cleanup
                conn.rollback()
                logger.error(
                    "Failed to batch create %d tuples",
                    len(tuples),
                    exc_info=True,
                )
                raise

        return created_count

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete_base(self, tuple_id: str) -> bool:
        """Delete a relationship tuple by its ID.

        Returns True if deleted, False if not found.
        """
        now = datetime.now(UTC).isoformat()

        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            if self._is_postgresql:
                cursor.execute(
                    self._fix_sql(
                        """
                        DELETE FROM rebac_tuples
                        WHERE tuple_id = ?
                          AND (expires_at IS NULL OR expires_at >= ?)
                        RETURNING
                            subject_type, subject_id, subject_relation,
                            relation, object_type, object_id, zone_id
                        """
                    ),
                    (tuple_id, now),
                )
                row = cursor.fetchone()
            else:
                cursor.execute(
                    self._fix_sql(
                        """
                        SELECT subject_type, subject_id, subject_relation,
                               relation, object_type, object_id, zone_id
                        FROM rebac_tuples
                        WHERE tuple_id = ?
                          AND (expires_at IS NULL OR expires_at >= ?)
                        """
                    ),
                    (tuple_id, now),
                )
                row = cursor.fetchone()

                if row:
                    cursor.execute(
                        self._fix_sql("DELETE FROM rebac_tuples WHERE tuple_id = ?"),
                        (tuple_id,),
                    )

            if not row:
                return False

            subject = Entity(row["subject_type"], row["subject_id"])
            subject_relation = row["subject_relation"]
            relation = row["relation"]
            obj = Entity(row["object_type"], row["object_id"])
            zone_id = row["zone_id"]

            cursor.execute(
                self._fix_sql(
                    """
                    INSERT INTO rebac_changelog (
                        change_type, tuple_id, subject_type, subject_id,
                        relation, object_type, object_id, zone_id, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    "DELETE",
                    tuple_id,
                    subject.entity_type,
                    subject.entity_id,
                    relation,
                    obj.entity_type,
                    obj.entity_id,
                    zone_id or ROOT_ZONE_ID,
                    now,
                ),
            )

            self._increment_zone_revision(zone_id, conn)
            conn.commit()
            self._set_tuple_version(self._get_tuple_version() + 1)

            self._invalidate_cache_for_tuple(
                subject, relation, obj, zone_id, subject_relation, conn=conn
            )

        return True

    # ------------------------------------------------------------------
    # Delete by subject (bulk)
    # ------------------------------------------------------------------

    def delete_by_subject(
        self,
        subject_type: str,
        subject_id: str,
        zone_id: str | None = None,
    ) -> int:
        """Delete all tuples for a given subject.

        Returns the number of tuples deleted.
        """
        normalized_zone = normalize_zone_id(zone_id)
        now = datetime.now(UTC).isoformat()
        fix = self._fix_sql

        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            select_q = (
                "SELECT tuple_id, subject_type, subject_id, relation, "
                "object_type, object_id, zone_id "
                "FROM rebac_tuples "
                "WHERE subject_type = ? AND subject_id = ?"
            )
            params: list[Any] = [subject_type, subject_id]
            if normalized_zone:
                select_q += " AND zone_id = ?"
                params.append(normalized_zone)
            cursor.execute(fix(select_q), params)
            rows = cursor.fetchall()

            if not rows:
                return 0

            delete_q = "DELETE FROM rebac_tuples WHERE subject_type = ? AND subject_id = ?"
            delete_params: list[Any] = [subject_type, subject_id]
            if normalized_zone:
                delete_q += " AND zone_id = ?"
                delete_params.append(normalized_zone)
            cursor.execute(fix(delete_q), delete_params)

            for row in rows:
                cursor.execute(
                    fix(
                        "INSERT INTO rebac_changelog ("
                        "  change_type, tuple_id, subject_type, subject_id,"
                        "  relation, object_type, object_id, zone_id, created_at"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    ),
                    (
                        "DELETE",
                        row["tuple_id"],
                        row["subject_type"],
                        row["subject_id"],
                        row["relation"],
                        row["object_type"],
                        row["object_id"],
                        row["zone_id"] or ROOT_ZONE_ID,
                        now,
                    ),
                )

            self._increment_zone_revision(zone_id, conn)
            conn.commit()
            self._set_tuple_version(self._get_tuple_version() + 1)

        return len(rows)

    # ------------------------------------------------------------------
    # Tiger write-through (read path)
    # ------------------------------------------------------------------

    def tiger_write_through_single(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        zone_id: str,
        tiger_cache: Any | None,
    ) -> None:
        """Write-through single permission result to Tiger Cache.

        Called after a single permission check computes a positive result.
        Non-blocking on the read path — only updates if resource already
        exists in the in-memory bitmap cache.
        """
        if not tiger_cache:
            return

        try:
            resource_key = (object[0], object[1])
            resource_int_id = tiger_cache._resource_map._uuid_to_int.get(resource_key)

            if resource_int_id is not None:
                tiger_cache.add_to_bitmap(
                    subject_type=subject[0],
                    subject_id=subject[1],
                    permission=permission,
                    resource_type=object[0],
                    zone_id=zone_id,
                    resource_int_id=resource_int_id,
                )
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "[TIGER] Read write-through: %s:%s %s %s:%s (int_id=%s)",
                        subject[0],
                        subject[1],
                        permission,
                        object[0],
                        object[1],
                        resource_int_id,
                    )
            else:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "[TIGER] Read skip: resource %s not in memory cache",
                        object[1],
                    )
        except (RuntimeError, ValueError, KeyError) as e:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("[TIGER] Write-through failed: %s", e)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _check_existing_tuple(
        self,
        cursor: Any,
        subject_entity: Entity,
        subject_relation: str | None,
        relation: str,
        object_entity: Entity,
        zone_id: str | None,
    ) -> str | None:
        """Check if a tuple already exists (idempotency). Returns tuple_id or None."""
        cursor.execute(
            self._fix_sql(
                """
                SELECT tuple_id FROM rebac_tuples
                WHERE subject_type = ? AND subject_id = ?
                AND (subject_relation = ? OR (subject_relation IS NULL AND ? IS NULL))
                AND relation = ?
                AND object_type = ? AND object_id = ?
                AND (zone_id = ? OR (zone_id IS NULL AND ? IS NULL))
                """
            ),
            (
                subject_entity.entity_type,
                subject_entity.entity_id,
                subject_relation,
                subject_relation,
                relation,
                object_entity.entity_type,
                object_entity.entity_id,
                zone_id,
                zone_id,
            ),
        )
        existing = cursor.fetchone()
        if existing:
            return cast(str, existing[0] if isinstance(existing, tuple) else existing["tuple_id"])
        return None


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _parse_subject(
    subject: tuple[str, str] | tuple[str, str, str],
) -> tuple[Entity, str | None]:
    """Parse a subject tuple into (Entity, subject_relation)."""
    if len(subject) == 3:
        subject_type, subject_id, subject_relation = subject
        return Entity(subject_type, subject_id), subject_relation
    if len(subject) == 2:
        subject_type, subject_id = subject
        return Entity(subject_type, subject_id), None
    raise ValueError(f"subject must be 2-tuple or 3-tuple, got {len(subject)}-tuple")
