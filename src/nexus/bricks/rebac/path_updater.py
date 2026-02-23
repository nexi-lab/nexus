"""Path Updater — handles rename/move of files and directories in ReBAC tuples.

Extracts the 426-LOC ``update_object_path`` from ``ReBACManager`` into
a focused module with sub-methods for object-id updates, subject-id updates,
Tiger resource map cleanup, and cache invalidation.

Related: Issue #2179 (decomposition), Issue #590 (batch UPDATE)
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from nexus.bricks.rebac.domain import Entity
from nexus.contracts.constants import ROOT_ZONE_ID

logger = logging.getLogger(__name__)


class PathUpdater:
    """Updates object_id / subject_id in ``rebac_tuples`` on file rename/move.

    Constructor dependencies mirror the thin helpers formerly on ReBACManager.
    """

    def __init__(
        self,
        connection_factory: Callable[..., Any],
        create_cursor: Callable[[Any], Any],
        fix_sql: Callable[[str], str],
        invalidate_cache_cb: Callable[..., None],
        tiger_invalidate_cache_cb: Callable[..., Any] | None,
        tiger_cache: Any | None,
    ) -> None:
        self._connection = connection_factory
        self._create_cursor = create_cursor
        self._fix_sql = fix_sql
        self._invalidate_cache = invalidate_cache_cb
        self._tiger_invalidate_cache = tiger_invalidate_cache_cb
        self._tiger_cache = tiger_cache

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_object_path(
        self,
        old_path: str,
        new_path: str,
        object_type: str = "file",
        is_directory: bool = False,
    ) -> tuple[int, bool]:
        """Update paths in rebac_tuples.

        Returns:
            (updated_count, should_bump_version) — caller uses
            ``should_bump_version`` to decide whether to increment
            ``_tuple_version``.
        """
        updated_count = 0

        logger.info(
            "update_object_path: %s -> %s, object_type=%s, is_directory=%s",
            old_path,
            new_path,
            object_type,
            is_directory,
        )

        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            # STEP 1: object_id column
            step1 = self._update_object_id_tuples(
                cursor,
                conn,
                old_path,
                new_path,
                object_type,
                is_directory,
            )
            updated_count += step1

            # STEP 2: subject_id column
            step2 = self._update_subject_id_tuples(
                cursor,
                conn,
                old_path,
                new_path,
                object_type,
                is_directory,
            )
            updated_count += step2

            # Tiger resource map cleanup
            self._cleanup_tiger_resource_map(
                cursor,
                old_path,
                object_type,
                is_directory,
            )

            conn.commit()
            logger.info("update_object_path complete: updated %d tuples total", updated_count)

        return updated_count, updated_count > 0

    # ------------------------------------------------------------------
    # STEP 1 — object_id updates
    # ------------------------------------------------------------------

    def _update_object_id_tuples(
        self,
        cursor: Any,
        conn: Any,
        old_path: str,
        new_path: str,
        object_type: str,
        is_directory: bool,
    ) -> int:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("STEP 1: Looking for tuples with object_id matching %s", old_path)

        now_iso = datetime.now(UTC).isoformat()

        if is_directory:
            cursor.execute(
                self._fix_sql(
                    """
                    SELECT tuple_id, subject_type, subject_id, subject_relation,
                           relation, object_type, object_id, zone_id
                    FROM rebac_tuples
                    WHERE object_type = ?
                      AND (object_id = ? OR object_id LIKE ?)
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                ),
                (object_type, old_path, old_path + "/%", now_iso),
            )
        else:
            cursor.execute(
                self._fix_sql(
                    """
                    SELECT tuple_id, subject_type, subject_id, subject_relation,
                           relation, object_type, object_id, zone_id
                    FROM rebac_tuples
                    WHERE object_type = ?
                      AND object_id = ?
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                ),
                (object_type, old_path, now_iso),
            )

        rows = cursor.fetchall()
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("update_object_path: Found %d tuples with object_id to update", len(rows))

        if not rows:
            return 0

        old_prefix_len = len(old_path)
        now_iso = datetime.now(UTC).isoformat()

        # Batch UPDATE
        if is_directory:
            cursor.execute(
                self._fix_sql(
                    """
                    UPDATE rebac_tuples
                    SET object_id = CASE
                        WHEN object_id = ? THEN ?
                        ELSE ? || SUBSTR(object_id, ?)
                    END
                    WHERE object_type = ?
                      AND (object_id = ? OR object_id LIKE ?)
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                ),
                (
                    old_path,
                    new_path,
                    new_path,
                    old_prefix_len + 1,
                    object_type,
                    old_path,
                    old_path + "/%",
                    now_iso,
                ),
            )
        else:
            cursor.execute(
                self._fix_sql(
                    """
                    UPDATE rebac_tuples
                    SET object_id = ?
                    WHERE object_type = ?
                      AND object_id = ?
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                ),
                (new_path, object_type, old_path, now_iso),
            )

        # Batch changelog INSERT
        changelog_entries = []
        for row in rows:
            old_object_id = row["object_id"]
            new_object_id = (
                new_path + old_object_id[old_prefix_len:]
                if is_directory and old_object_id.startswith(old_path + "/")
                else new_path
            )
            changelog_entries.append(
                (
                    "UPDATE",
                    row["tuple_id"],
                    row["subject_type"],
                    row["subject_id"],
                    row["relation"],
                    object_type,
                    new_object_id,
                    row["zone_id"] or ROOT_ZONE_ID,
                    now_iso,
                )
            )

        cursor.executemany(
            self._fix_sql(
                """
                INSERT INTO rebac_changelog (
                    change_type, tuple_id, subject_type, subject_id,
                    relation, object_type, object_id, zone_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
            ),
            changelog_entries,
        )

        # Cache invalidation
        for row in rows:
            old_object_id = row["object_id"]
            new_object_id = (
                new_path + old_object_id[old_prefix_len:]
                if is_directory and old_object_id.startswith(old_path + "/")
                else new_path
            )

            subject = Entity(row["subject_type"], row["subject_id"])
            old_obj = Entity(object_type, old_object_id)
            new_obj = Entity(object_type, new_object_id)
            relation = row["relation"]
            zone_id = row["zone_id"]
            subject_relation = row["subject_relation"]

            self._invalidate_cache(subject, relation, old_obj, zone_id, subject_relation, conn=conn)

            # Tiger Cache invalidation for the subject (PR #969)
            if self._tiger_invalidate_cache is not None:
                try:
                    self._tiger_invalidate_cache(
                        subject=(subject.entity_type, subject.entity_id),
                        resource_type=old_obj.entity_type,
                        zone_id=zone_id or ROOT_ZONE_ID,
                    )
                except (RuntimeError, ValueError, KeyError, OSError) as e:
                    logger.warning("Tiger Cache invalidation failed during rename: %s", e)

            self._invalidate_cache(subject, relation, new_obj, zone_id, subject_relation, conn=conn)

        return len(rows)

    # ------------------------------------------------------------------
    # STEP 2 — subject_id updates
    # ------------------------------------------------------------------

    def _update_subject_id_tuples(
        self,
        cursor: Any,
        conn: Any,
        old_path: str,
        new_path: str,
        object_type: str,
        is_directory: bool,
    ) -> int:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("STEP 2: Looking for tuples with subject_id matching %s", old_path)

        now_iso = datetime.now(UTC).isoformat()

        if is_directory:
            cursor.execute(
                self._fix_sql(
                    """
                    SELECT tuple_id, subject_type, subject_id, subject_relation,
                           relation, object_type, object_id, zone_id
                    FROM rebac_tuples
                    WHERE subject_type = ?
                      AND (subject_id = ? OR subject_id LIKE ?)
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                ),
                (object_type, old_path, old_path + "/%", now_iso),
            )
        else:
            cursor.execute(
                self._fix_sql(
                    """
                    SELECT tuple_id, subject_type, subject_id, subject_relation,
                           relation, object_type, object_id, zone_id
                    FROM rebac_tuples
                    WHERE subject_type = ?
                      AND subject_id = ?
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                ),
                (object_type, old_path, now_iso),
            )

        subject_rows = cursor.fetchall()
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "update_object_path: Found %d tuples with subject_id to update",
                len(subject_rows),
            )

        if not subject_rows:
            return 0

        old_prefix_len = len(old_path)
        now_iso = datetime.now(UTC).isoformat()

        # Batch UPDATE
        if is_directory:
            cursor.execute(
                self._fix_sql(
                    """
                    UPDATE rebac_tuples
                    SET subject_id = CASE
                        WHEN subject_id = ? THEN ?
                        ELSE ? || SUBSTR(subject_id, ?)
                    END
                    WHERE subject_type = ?
                      AND (subject_id = ? OR subject_id LIKE ?)
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                ),
                (
                    old_path,
                    new_path,
                    new_path,
                    old_prefix_len + 1,
                    object_type,
                    old_path,
                    old_path + "/%",
                    now_iso,
                ),
            )
        else:
            cursor.execute(
                self._fix_sql(
                    """
                    UPDATE rebac_tuples
                    SET subject_id = ?
                    WHERE subject_type = ?
                      AND subject_id = ?
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                ),
                (new_path, object_type, old_path, now_iso),
            )

        # Batch changelog INSERT
        changelog_entries = []
        for row in subject_rows:
            old_subject_id = row["subject_id"]
            new_subject_id = (
                new_path + old_subject_id[old_prefix_len:]
                if is_directory and old_subject_id.startswith(old_path + "/")
                else new_path
            )
            changelog_entries.append(
                (
                    "UPDATE",
                    row["tuple_id"],
                    object_type,
                    new_subject_id,
                    row["relation"],
                    row["object_type"],
                    row["object_id"],
                    row["zone_id"] or ROOT_ZONE_ID,
                    now_iso,
                )
            )

        cursor.executemany(
            self._fix_sql(
                """
                INSERT INTO rebac_changelog (
                    change_type, tuple_id, subject_type, subject_id,
                    relation, object_type, object_id, zone_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
            ),
            changelog_entries,
        )

        # Cache invalidation
        for row in subject_rows:
            old_subject_id = row["subject_id"]
            new_subject_id = (
                new_path + old_subject_id[old_prefix_len:]
                if is_directory and old_subject_id.startswith(old_path + "/")
                else new_path
            )
            old_subj = Entity(object_type, old_subject_id)
            new_subj = Entity(object_type, new_subject_id)
            obj = Entity(row["object_type"], row["object_id"])
            relation = row["relation"]
            zone_id = row["zone_id"]
            subject_relation = row["subject_relation"]

            self._invalidate_cache(old_subj, relation, obj, zone_id, subject_relation, conn=conn)
            self._invalidate_cache(new_subj, relation, obj, zone_id, subject_relation, conn=conn)

        return len(subject_rows)

    # ------------------------------------------------------------------
    # Tiger resource map cleanup
    # ------------------------------------------------------------------

    def _cleanup_tiger_resource_map(
        self,
        cursor: Any,
        old_path: str,
        object_type: str,
        is_directory: bool,
    ) -> None:
        if self._tiger_cache is None:
            return

        try:
            if is_directory:
                cursor.execute(
                    self._fix_sql(
                        """
                        DELETE FROM tiger_resource_map
                        WHERE resource_type = ?
                          AND (resource_id = ? OR resource_id LIKE ?)
                        """
                    ),
                    (object_type, old_path, old_path + "/%"),
                )
            else:
                cursor.execute(
                    self._fix_sql(
                        """
                        DELETE FROM tiger_resource_map
                        WHERE resource_type = ? AND resource_id = ?
                        """
                    ),
                    (object_type, old_path),
                )
            deleted = cursor.rowcount
            if deleted and deleted > 0:
                logger.info(
                    "[UPDATE-OBJECT-PATH] Deleted %d entries from tiger_resource_map",
                    deleted,
                )

            # Clear in-memory resource map cache
            resource_map = self._tiger_cache._resource_map
            if hasattr(resource_map, "_uuid_to_int"):
                keys_to_remove = []
                for key in resource_map._uuid_to_int:
                    res_type, res_id = key
                    if res_type == object_type:
                        if is_directory:
                            if res_id == old_path or res_id.startswith(old_path + "/"):
                                keys_to_remove.append(key)
                        elif res_id == old_path:
                            keys_to_remove.append(key)
                for key in keys_to_remove:
                    int_id = resource_map._uuid_to_int.pop(key, None)
                    if int_id is not None and hasattr(resource_map, "_int_to_uuid"):
                        resource_map._int_to_uuid.pop(int_id, None)
        except (RuntimeError, ValueError, KeyError, OSError) as e:
            logger.warning("[UPDATE-OBJECT-PATH] Failed to update tiger_resource_map: %s", e)
