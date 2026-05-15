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
        requested_zone_id = self._extract_zone_id(old_path) or self._extract_zone_id(new_path)

        # Normalize to the user-facing virtual path. Some tuples are stored
        # unscoped and others preserve /zone/{id}/..., so the rewrite logic
        # below updates whichever representation each tuple currently uses.
        try:
            from nexus.core.path_utils import unscope_internal_path

            old_path = unscope_internal_path(old_path)
            new_path = unscope_internal_path(new_path)
        except ImportError:
            pass  # path_utils not available outside server context

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
                requested_zone_id,
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
                requested_zone_id,
                object_type,
                is_directory,
            )
            updated_count += step2

            # Tiger resource map cleanup
            self._cleanup_tiger_resource_map(
                cursor,
                old_path,
                requested_zone_id,
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
        requested_zone_id: str | None,
        object_type: str,
        is_directory: bool,
    ) -> int:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("STEP 1: Looking for tuples with object_id matching %s", old_path)

        now_iso = datetime.now(UTC).isoformat()
        candidates = self._path_candidates(old_path, requested_zone_id)
        where_sql, params = self._build_path_match_clause("object_id", candidates, is_directory)
        zone_sql, zone_params = self._build_zone_clause(requested_zone_id)

        cursor.execute(
            self._fix_sql(
                f"""
                SELECT tuple_id, subject_type, subject_id, subject_relation,
                       relation, object_type, object_id, zone_id
                FROM rebac_tuples
                WHERE object_type = ?
                  AND ({where_sql})
                  {zone_sql}
                  AND (expires_at IS NULL OR expires_at >= ?)
                """
            ),
            (object_type, *params, *zone_params, now_iso),
        )

        rows = cursor.fetchall()
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("update_object_path: Found %d tuples with object_id to update", len(rows))

        if not rows:
            return 0

        now_iso = datetime.now(UTC).isoformat()

        # Batch changelog INSERT
        changelog_entries = []
        for row in rows:
            old_object_id = row["object_id"]
            new_object_id = self._rewrite_stored_path(
                old_object_id,
                old_path,
                new_path,
                row["zone_id"],
                is_directory,
            )
            if new_object_id is None:
                logger.warning(
                    "Skipping object_id tuple %s during rename; no rewrite match for %s",
                    row["tuple_id"],
                    old_object_id,
                )
                continue
            cursor.execute(
                self._fix_sql(
                    """
                    UPDATE rebac_tuples
                    SET object_id = ?
                    WHERE tuple_id = ?
                    """
                ),
                (new_object_id, row["tuple_id"]),
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

        if changelog_entries:
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
            new_object_id = self._rewrite_stored_path(
                old_object_id,
                old_path,
                new_path,
                row["zone_id"],
                is_directory,
            )
            if new_object_id is None:
                continue

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

        return len(changelog_entries)

    # ------------------------------------------------------------------
    # STEP 2 — subject_id updates
    # ------------------------------------------------------------------

    def _update_subject_id_tuples(
        self,
        cursor: Any,
        conn: Any,
        old_path: str,
        new_path: str,
        requested_zone_id: str | None,
        object_type: str,
        is_directory: bool,
    ) -> int:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("STEP 2: Looking for tuples with subject_id matching %s", old_path)

        now_iso = datetime.now(UTC).isoformat()
        candidates = self._path_candidates(old_path, requested_zone_id)
        where_sql, params = self._build_path_match_clause("subject_id", candidates, is_directory)
        zone_sql, zone_params = self._build_zone_clause(requested_zone_id)

        cursor.execute(
            self._fix_sql(
                f"""
                SELECT tuple_id, subject_type, subject_id, subject_relation,
                       relation, object_type, object_id, zone_id
                FROM rebac_tuples
                WHERE subject_type = ?
                  AND ({where_sql})
                  {zone_sql}
                  AND (expires_at IS NULL OR expires_at >= ?)
                """
            ),
            (object_type, *params, *zone_params, now_iso),
        )

        subject_rows = cursor.fetchall()
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "update_object_path: Found %d tuples with subject_id to update",
                len(subject_rows),
            )

        if not subject_rows:
            return 0

        now_iso = datetime.now(UTC).isoformat()

        # Batch changelog INSERT
        changelog_entries = []
        for row in subject_rows:
            old_subject_id = row["subject_id"]
            new_subject_id = self._rewrite_stored_path(
                old_subject_id,
                old_path,
                new_path,
                row["zone_id"],
                is_directory,
            )
            if new_subject_id is None:
                logger.warning(
                    "Skipping subject_id tuple %s during rename; no rewrite match for %s",
                    row["tuple_id"],
                    old_subject_id,
                )
                continue
            cursor.execute(
                self._fix_sql(
                    """
                    UPDATE rebac_tuples
                    SET subject_id = ?
                    WHERE tuple_id = ?
                    """
                ),
                (new_subject_id, row["tuple_id"]),
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

        if changelog_entries:
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
            new_subject_id = self._rewrite_stored_path(
                old_subject_id,
                old_path,
                new_path,
                row["zone_id"],
                is_directory,
            )
            if new_subject_id is None:
                continue
            old_subj = Entity(object_type, old_subject_id)
            new_subj = Entity(object_type, new_subject_id)
            obj = Entity(row["object_type"], row["object_id"])
            relation = row["relation"]
            zone_id = row["zone_id"]
            subject_relation = row["subject_relation"]

            self._invalidate_cache(old_subj, relation, obj, zone_id, subject_relation, conn=conn)
            self._invalidate_cache(new_subj, relation, obj, zone_id, subject_relation, conn=conn)

        return len(changelog_entries)

    # ------------------------------------------------------------------
    # Tiger resource map cleanup
    # ------------------------------------------------------------------

    def _cleanup_tiger_resource_map(
        self,
        cursor: Any,
        old_path: str,
        requested_zone_id: str | None,
        object_type: str,
        is_directory: bool,
    ) -> None:
        if self._tiger_cache is None:
            return

        try:
            candidates = self._path_candidates(old_path, requested_zone_id)
            where_sql, params = self._build_path_match_clause(
                "resource_id", candidates, is_directory
            )
            # tiger_resource_map intentionally has NO zone_id column (migration
            # tiger_resource_map_remove_tenant dropped the tenant axis because
            # resource paths are globally unique). Emitting `AND zone_id = ?`
            # here makes postgres reject the DELETE with
            # `column "zone_id" does not exist`, so stale (int_id ↔ path)
            # entries never get cleaned up after rename/move. Omit the clause.
            cursor.execute(
                self._fix_sql(
                    f"""
                    DELETE FROM tiger_resource_map
                    WHERE resource_type = ?
                      AND ({where_sql})
                    """
                ),
                (object_type, *params),
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
                    if res_type == object_type and self._matches_any_candidate(
                        res_id, candidates, is_directory
                    ):
                        keys_to_remove.append(key)
                for key in keys_to_remove:
                    int_id = resource_map._uuid_to_int.pop(key, None)
                    if int_id is not None and hasattr(resource_map, "_int_to_uuid"):
                        resource_map._int_to_uuid.pop(int_id, None)
        except (RuntimeError, ValueError, KeyError, OSError) as e:
            logger.warning("[UPDATE-OBJECT-PATH] Failed to update tiger_resource_map: %s", e)

    def _extract_zone_id(self, path: str) -> str | None:
        if not path.startswith("/zone/"):
            return None
        parts = path.split("/", 3)
        if len(parts) < 3 or not parts[2]:
            return None
        return parts[2]

    def _scope_path_for_zone(self, path: str, zone_id: str | None) -> str | None:
        if not zone_id or zone_id == ROOT_ZONE_ID:
            return None
        return f"/zone/{zone_id}{path}"

    def _path_candidates(self, path: str, zone_id: str | None) -> list[str]:
        candidates = [path]
        scoped = self._scope_path_for_zone(path, zone_id)
        if scoped and scoped not in candidates:
            candidates.append(scoped)
        return candidates

    def _matches_any_candidate(
        self,
        stored_path: str,
        candidates: list[str],
        is_directory: bool,
    ) -> bool:
        for candidate in candidates:
            if stored_path == candidate:
                return True
            if is_directory and stored_path.startswith(candidate + "/"):
                return True
        return False

    def _rewrite_stored_path(
        self,
        stored_path: str,
        old_path: str,
        new_path: str,
        zone_id: str | None,
        is_directory: bool,
    ) -> str | None:
        old_candidates = self._path_candidates(old_path, zone_id)
        new_candidates = self._path_candidates(new_path, zone_id)

        for old_candidate, new_candidate in zip(old_candidates, new_candidates, strict=False):
            if stored_path == old_candidate:
                return new_candidate
            if is_directory and stored_path.startswith(old_candidate + "/"):
                return new_candidate + stored_path[len(old_candidate) :]
        return None

    def _build_path_match_clause(
        self,
        column: str,
        candidates: list[str],
        is_directory: bool,
    ) -> tuple[str, list[str]]:
        clauses: list[str] = []
        params: list[str] = []
        for candidate in candidates:
            if is_directory:
                clauses.append(f"({column} = ? OR {column} LIKE ?)")
                params.extend([candidate, candidate + "/%"])
            else:
                clauses.append(f"{column} = ?")
                params.append(candidate)
        return " OR ".join(clauses), params

    def _build_zone_clause(self, zone_id: str | None) -> tuple[str, list[str]]:
        if zone_id is None:
            return "", []
        if zone_id == ROOT_ZONE_ID:
            return " AND (zone_id = ? OR zone_id IS NULL)", [ROOT_ZONE_ID]
        return " AND zone_id = ?", [zone_id]
