"""ReBAC Share Mixin - Extracted from ReBACService (Issue #1287, 8A).

This mixin provides privacy, sharing, dynamic viewer, and Tiger cache methods:
- Privacy & consent management
- Cross-zone sharing (user/group)
- Share revocation and listing
- Dynamic viewer column-level filtering
- Tiger cache warming and traverse grants

Extracted from: rebac_service.py (2,291 lines -> ~1,810 remaining)
"""

import logging
from datetime import datetime
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID

logger = logging.getLogger(__name__)


class ReBACShareMixin:
    """Mixin providing sharing, privacy, and Tiger cache methods for ReBACService.

    Accesses ReBACService attributes via ``self``:
    - _rebac_manager, _require_manager()
    - _check_share_permission()
    - rebac_expand_sync(), rebac_create_sync(), rebac_delete_sync()
    - rebac_list_tuples_sync()
    """

    # =========================================================================
    # Sync: Privacy & Consent
    # =========================================================================

    def rebac_expand_with_privacy_sync(
        self,
        permission: str,
        object: tuple[str, str],
        respect_consent: bool = True,
        requester: tuple[str, str] | None = None,
    ) -> list[tuple[str, str]]:
        """Expand permissions with privacy filtering (sync)."""
        all_subjects = self.rebac_expand_sync(permission, object)
        if not respect_consent or not requester:
            return all_subjects

        mgr = self._require_manager()
        filtered = []
        for subj in all_subjects:
            can_discover = mgr.rebac_check(subject=requester, permission="discover", object=subj)
            if can_discover:
                filtered.append(subj)
        return filtered

    def grant_consent_sync(
        self,
        from_subject: tuple[str, str],
        to_subject: tuple[str, str],
        expires_at: datetime | None = None,
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        """Grant consent for discovery (sync)."""
        return self.rebac_create_sync(
            subject=to_subject,
            relation="consent_granted",
            object=from_subject,
            expires_at=expires_at,
            zone_id=zone_id,
        )

    def revoke_consent_sync(
        self, from_subject: tuple[str, str], to_subject: tuple[str, str]
    ) -> bool:
        """Revoke previously granted consent (sync)."""
        tuples = self.rebac_list_tuples_sync(
            subject=to_subject, relation="consent_granted", object=from_subject
        )
        if tuples:
            return self.rebac_delete_sync(tuples[0]["tuple_id"])
        return False

    def make_public_sync(
        self, resource: tuple[str, str], zone_id: str | None = None
    ) -> dict[str, Any]:
        """Make a resource publicly discoverable (sync)."""
        return self.rebac_create_sync(
            subject=("*", "*"),
            relation="public_discoverable",
            object=resource,
            zone_id=zone_id,
        )

    def make_private_sync(self, resource: tuple[str, str]) -> bool:
        """Remove public discoverability from a resource (sync)."""
        tuples = self.rebac_list_tuples_sync(
            subject=("*", "*"), relation="public_discoverable", object=resource
        )
        if tuples:
            return self.rebac_delete_sync(tuples[0]["tuple_id"])
        return False

    # =========================================================================
    # Sync: Cross-Zone Sharing
    # =========================================================================

    def share_with_user_sync(
        self,
        resource: tuple[str, str],
        user_id: str,
        relation: str = "viewer",
        zone_id: str | None = None,
        user_zone_id: str | None = None,
        expires_at: datetime | None = None,
        context: Any = None,
    ) -> dict[str, Any]:
        """Share a resource with a specific user (sync)."""
        mgr = self._require_manager()

        self._check_share_permission(resource=resource, context=context)

        relation_map = {
            "viewer": "shared-viewer",
            "editor": "shared-editor",
            "owner": "shared-owner",
        }
        if relation not in relation_map:
            raise ValueError(f"relation must be 'viewer', 'editor', or 'owner', got '{relation}'")
        tuple_relation = relation_map[relation]

        expires_dt = None
        if expires_at is not None:
            if isinstance(expires_at, str):
                from datetime import datetime as dt

                expires_dt = dt.fromisoformat(expires_at.replace("Z", "+00:00"))
            else:
                expires_dt = expires_at

        result = mgr.rebac_write(
            subject=("user", user_id),
            relation=tuple_relation,
            object=resource,
            zone_id=zone_id,
            subject_zone_id=user_zone_id,
            expires_at=expires_dt,
        )
        return {
            "tuple_id": result.tuple_id,
            "revision": result.revision,
            "consistency_token": result.consistency_token,
        }

    def share_with_group_sync(
        self,
        resource: tuple[str, str],
        group_id: str,
        relation: str = "viewer",
        zone_id: str | None = None,
        group_zone_id: str | None = None,
        expires_at: datetime | None = None,
        context: Any = None,
    ) -> dict[str, Any]:
        """Share a resource with a group (sync)."""
        mgr = self._require_manager()

        self._check_share_permission(resource=resource, context=context)

        relation_map = {
            "viewer": "shared-viewer",
            "editor": "shared-editor",
            "owner": "shared-owner",
        }
        if relation not in relation_map:
            raise ValueError(f"relation must be 'viewer', 'editor', or 'owner', got '{relation}'")
        tuple_relation = relation_map[relation]

        expires_dt = None
        if expires_at is not None:
            if isinstance(expires_at, str):
                from datetime import datetime as dt

                expires_dt = dt.fromisoformat(expires_at.replace("Z", "+00:00"))
            else:
                expires_dt = expires_at

        result = mgr.rebac_write(
            subject=("group", group_id, "member"),
            relation=tuple_relation,
            object=resource,
            zone_id=zone_id,
            subject_zone_id=group_zone_id,
            expires_at=expires_dt,
        )
        return {
            "tuple_id": result.tuple_id,
            "revision": result.revision,
            "consistency_token": result.consistency_token,
        }

    def revoke_share_sync(
        self,
        resource: tuple[str, str],
        user_id: str,
    ) -> bool:
        """Revoke a share for a specific user on a resource (sync)."""
        tuples = self.rebac_list_tuples_sync(
            subject=("user", user_id),
            relation_in=["shared-viewer", "shared-editor", "shared-owner"],
            object=resource,
        )
        if tuples:
            return self.rebac_delete_sync(tuples[0]["tuple_id"])
        return False

    def revoke_share_by_id_sync(self, share_id: str) -> bool:
        """Revoke a share using its ID (sync)."""
        return self.rebac_delete_sync(share_id)

    def list_outgoing_shares_sync(
        self,
        resource: tuple[str, str] | None = None,
        zone_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        cursor: str | None = None,
        current_zone: str = ROOT_ZONE_ID,
    ) -> dict[str, Any]:
        """List outgoing shares with iterator caching (sync)."""
        mgr = self._require_manager()
        if zone_id is not None:
            current_zone = zone_id

        from nexus.bricks.rebac.cache.iterator import CursorExpiredError

        relation_to_level = {
            "shared-viewer": "viewer",
            "shared-editor": "editor",
            "shared-owner": "owner",
        }

        def _transform(tuples: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                {
                    "share_id": t.get("tuple_id"),
                    "resource_type": t.get("object_type"),
                    "resource_id": t.get("object_id"),
                    "recipient_id": t.get("subject_id"),
                    "permission_level": relation_to_level.get(t.get("relation") or "", "viewer"),
                    "created_at": t.get("created_at"),
                    "expires_at": t.get("expires_at"),
                }
                for t in tuples
            ]

        def _compute() -> list[dict[str, Any]]:
            all_tuples = self.rebac_list_tuples_sync(
                relation_in=["shared-viewer", "shared-editor", "shared-owner"],
                object=resource,
            )
            return _transform(all_tuples)

        if cursor:
            try:
                items, next_cursor, total = mgr._iterator_cache.get_page(
                    cursor_id=cursor,
                    offset=offset,
                    limit=limit,
                )
                return {
                    "items": items,
                    "next_cursor": next_cursor,
                    "total_count": total,
                    "has_more": next_cursor is not None,
                }
            except CursorExpiredError:
                pass

        resource_str = f"{resource[0]}:{resource[1]}" if resource else "all"
        query_hash = f"outgoing:{current_zone}:{resource_str}"

        cursor_id, all_results, total = mgr._iterator_cache.get_or_create(
            query_hash=query_hash,
            zone_id=current_zone,
            compute_fn=_compute,
        )

        items = all_results[offset : offset + limit]
        has_more = offset + limit < total
        next_cursor_val = cursor_id if has_more else None

        return {
            "items": items,
            "next_cursor": next_cursor_val,
            "total_count": total,
            "has_more": has_more,
        }

    def list_incoming_shares_sync(
        self,
        user_id: str,
        limit: int = 100,
        offset: int = 0,
        cursor: str | None = None,
        current_zone: str = ROOT_ZONE_ID,
    ) -> dict[str, Any]:
        """List incoming shares with iterator caching (sync)."""
        mgr = self._require_manager()

        from nexus.bricks.rebac.cache.iterator import CursorExpiredError

        relation_to_level = {
            "shared-viewer": "viewer",
            "shared-editor": "editor",
            "shared-owner": "owner",
        }

        def _transform(tuples: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                {
                    "share_id": t.get("tuple_id"),
                    "resource_type": t.get("object_type"),
                    "resource_id": t.get("object_id"),
                    "owner_zone_id": t.get("zone_id"),
                    "permission_level": relation_to_level.get(t.get("relation") or "", "viewer"),
                    "created_at": t.get("created_at"),
                    "expires_at": t.get("expires_at"),
                }
                for t in tuples
            ]

        def _compute() -> list[dict[str, Any]]:
            all_tuples = self.rebac_list_tuples_sync(
                subject=("user", user_id),
                relation_in=["shared-viewer", "shared-editor", "shared-owner"],
            )
            return _transform(all_tuples)

        if cursor:
            try:
                items, next_cursor, total = mgr._iterator_cache.get_page(
                    cursor_id=cursor,
                    offset=offset,
                    limit=limit,
                )
                return {
                    "items": items,
                    "next_cursor": next_cursor,
                    "total_count": total,
                    "has_more": next_cursor is not None,
                }
            except CursorExpiredError:
                pass

        query_hash = f"incoming:{current_zone}:{user_id}"

        cursor_id, all_results, total = mgr._iterator_cache.get_or_create(
            query_hash=query_hash,
            zone_id=current_zone,
            compute_fn=_compute,
        )

        items = all_results[offset : offset + limit]
        has_more = offset + limit < total
        next_cursor_val = cursor_id if has_more else None

        return {
            "items": items,
            "next_cursor": next_cursor_val,
            "total_count": total,
            "has_more": has_more,
        }

    # =========================================================================
    # Sync: Dynamic Viewer
    # =========================================================================

    def get_dynamic_viewer_config_sync(
        self,
        subject: tuple[str, str],
        file_path: str,
    ) -> dict[str, Any] | None:
        """Get dynamic_viewer configuration for a subject and file (sync)."""
        import json as _json

        mgr = self._require_manager()

        tuples = self.rebac_list_tuples_sync(
            subject=subject, relation="dynamic_viewer", object=("file", file_path)
        )
        if not tuples:
            return None

        tuple_data = tuples[0]
        conn = mgr._get_connection()
        try:
            cursor = mgr._create_cursor(conn)
            cursor.execute(
                mgr._fix_sql_placeholders("SELECT conditions FROM rebac_tuples WHERE tuple_id = ?"),
                (tuple_data["tuple_id"],),
            )
            row = cursor.fetchone()
            if row and row["conditions"]:
                conditions = _json.loads(row["conditions"])
                if conditions.get("type") == "dynamic_viewer":
                    col_cfg: dict[str, Any] | None = conditions.get("column_config")
                    return col_cfg
        finally:
            mgr._close_connection(conn)
        return None

    def apply_dynamic_viewer_filter_sync(
        self,
        data: str,
        column_config: dict[str, Any],
        file_format: str = "csv",
    ) -> dict[str, Any]:
        """Apply column-level filtering and aggregations to CSV data (sync)."""
        if file_format != "csv":
            raise ValueError(f"Unsupported file format: {file_format}. Only 'csv' is supported.")

        try:
            import io

            import pandas as pd
        except ImportError as e:
            raise RuntimeError(
                "pandas is required for dynamic viewer filtering. Install with: pip install pandas"
            ) from e

        try:
            df = pd.read_csv(io.StringIO(data))
        except (ValueError, pd.errors.ParserError) as e:
            raise RuntimeError(f"Failed to parse CSV data: {e}") from e

        hidden_columns = column_config.get("hidden_columns", [])
        aggregations = column_config.get("aggregations", {})
        visible_columns = column_config.get("visible_columns", [])

        if not visible_columns:
            all_cols = set(df.columns)
            hidden_set = set(hidden_columns)
            agg_set = set(aggregations.keys())
            visible_columns = list(all_cols - hidden_set - agg_set)

        result_columns: list[tuple[str, Any]] = []
        aggregation_results: dict[str, dict[str, float | int | str]] = {}
        aggregated_column_names: list[str] = []
        columns_shown: list[str] = []

        for col in df.columns:
            if col in hidden_columns:
                continue
            elif col in aggregations:
                operation = aggregations[col]
                try:
                    if operation == "mean":
                        agg_value = float(df[col].mean())
                    elif operation == "sum":
                        agg_value = float(df[col].sum())
                    elif operation == "count":
                        agg_value = int(df[col].count())
                    elif operation == "min":
                        agg_value = float(df[col].min())
                    elif operation == "max":
                        agg_value = float(df[col].max())
                    elif operation == "std":
                        agg_value = float(df[col].std())
                    elif operation == "median":
                        agg_value = float(df[col].median())
                    else:
                        continue

                    if col not in aggregation_results:
                        aggregation_results[col] = {}
                    aggregation_results[col][operation] = agg_value

                    agg_col_name = f"{operation}({col})"
                    aggregated_column_names.append(agg_col_name)
                    agg_series = pd.Series([agg_value] * len(df), name=agg_col_name)
                    result_columns.append((agg_col_name, agg_series))
                except (ValueError, TypeError, KeyError) as e:
                    if col not in aggregation_results:
                        aggregation_results[col] = {}
                    aggregation_results[col][operation] = f"error: {str(e)}"
            elif col in visible_columns:
                result_columns.append((col, df[col]))
                columns_shown.append(col)

        result_df = pd.DataFrame(dict(result_columns)) if result_columns else pd.DataFrame()
        filtered_data = result_df.to_csv(index=False)

        return {
            "filtered_data": filtered_data,
            "aggregations": aggregation_results,
            "columns_shown": columns_shown,
            "aggregated_columns": aggregated_column_names,
        }

    # =========================================================================
    # Sync: Tiger Cache & Traverse
    # =========================================================================

    def grant_traverse_on_implicit_dirs_sync(
        self,
        zone_id: str | None = None,
        subject: tuple[str, str] | None = None,
    ) -> list[Any]:
        """Grant TRAVERSE permission on root-level implicit directories (sync)."""
        from sqlalchemy.exc import OperationalError

        from nexus.bricks.rebac.utils.zone import normalize_zone_id

        mgr = self._require_manager()
        if subject is None:
            subject = ("group", "authenticated")
        effective_zone_id = normalize_zone_id(zone_id)

        implicit_dirs = [
            "/",
            "/zones",
            "/sessions",
            "/skills",
            "/workspace",
            "/shared",
            "/__sys__",
            "/archives",
            "/external",
        ]

        tuple_ids = []
        for dir_path in implicit_dirs:
            try:
                existing = self.rebac_list_tuples_sync(
                    subject=subject,
                    relation="traverser-of",
                    object=("file", dir_path),
                )
                if existing:
                    continue
                tuple_id = mgr.rebac_write(
                    subject=subject,
                    relation="traverser-of",
                    object=("file", dir_path),
                    zone_id=effective_zone_id,
                )
                tuple_ids.append(tuple_id)
            except (RuntimeError, ValueError, OperationalError) as e:
                logger.warning("Failed to grant TRAVERSE on %s: %s", dir_path, e)
        return tuple_ids

    def process_tiger_cache_queue_sync(self, batch_size: int = 100) -> int:
        """Process pending Tiger Cache update queue (sync)."""
        if not self._rebac_manager:
            return 0
        mgr = self._require_manager()
        if hasattr(mgr, "tiger_process_queue"):
            count: int = mgr.tiger_process_queue(batch_size=batch_size)
            return count
        return 0

    def warm_tiger_cache_sync(
        self,
        subjects: list[tuple[str, str]] | None = None,
        zone_id: str | None = None,
    ) -> int:
        """Warm the Tiger Cache by pre-computing permissions for subjects (sync)."""
        from sqlalchemy.exc import OperationalError

        from nexus.bricks.rebac.utils.zone import normalize_zone_id

        if not self._rebac_manager:
            return 0

        mgr = self._require_manager()
        effective_zone_id = normalize_zone_id(zone_id)
        entries_created = 0

        if subjects is None:
            try:
                tuples = self.rebac_list_tuples_sync()
                subjects_set: set[tuple[str, str]] = set()
                for t in tuples:
                    subject_type = t.get("subject_type")
                    subject_id = t.get("subject_id")
                    if subject_type and subject_id:
                        subjects_set.add((subject_type, subject_id))
                subjects = list(subjects_set)
            except (KeyError, TypeError, AttributeError):
                subjects = []

        for subj in subjects:
            if hasattr(mgr, "tiger_queue_update"):
                for permission in ["read", "write", "traverse"]:
                    mgr.tiger_queue_update(
                        subject=subj,
                        permission=permission,
                        resource_type="file",
                        zone_id=effective_zone_id,
                    )
                    entries_created += 1

        if hasattr(mgr, "tiger_process_queue"):
            try:
                mgr.tiger_process_queue(batch_size=5)
            except (RuntimeError, OperationalError) as e:
                logger.warning("[WARM-TIGER] Queue processing failed: %s", e)

        return entries_created
