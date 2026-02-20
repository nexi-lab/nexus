"""Leopard Facade — thin wrapper around the LeopardIndex.

Encapsulates all Leopard-related operations that were previously inlined
in ReBACManager: get/fallback transitive groups, rebuild, invalidate,
and update-on-write/delete.

Related: Issue #2179 (decomposition), Issue #692 (Leopard index)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import OperationalError, ProgrammingError

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from nexus.bricks.rebac.cache.leopard import LeopardIndex

logger = logging.getLogger(__name__)

# Relations that represent group membership
MEMBERSHIP_RELATIONS = frozenset({"member-of", "member", "belongs-to"})


class LeopardFacade:
    """Facade for Leopard transitive closure operations.

    Centralises leopard-related logic that was previously spread across
    ``ReBACManager.get_transitive_groups``, ``rebuild_leopard_closure``,
    ``invalidate_leopard_cache``, and the inline update blocks in
    ``rebac_write`` / ``rebac_delete``.
    """

    def __init__(
        self,
        leopard: LeopardIndex | None,
        engine: Engine,
        is_postgresql: bool = False,
    ) -> None:
        self._leopard = leopard
        self._engine = engine
        self._is_postgresql = is_postgresql

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def get_transitive_groups(
        self,
        subject_type: str,
        subject_id: str,
        zone_id: str,
    ) -> set[tuple[str, str]]:
        """Get all groups a subject transitively belongs to.

        Uses pre-computed transitive closure for O(1) lookup when the
        Leopard index is enabled, otherwise falls back to BFS traversal.
        """
        if not self._leopard:
            return self._compute_transitive_groups_fallback(subject_type, subject_id, zone_id)

        return self._leopard.get_transitive_groups(
            member_type=subject_type,
            member_id=subject_id,
            zone_id=zone_id,
        )

    def _compute_transitive_groups_fallback(
        self,
        subject_type: str,
        subject_id: str,
        zone_id: str,
    ) -> set[tuple[str, str]]:
        """Compute transitive groups via BFS (no Leopard index)."""
        from sqlalchemy import text

        groups: set[tuple[str, str]] = set()
        visited: set[tuple[str, str]] = set()
        queue: list[tuple[str, str]] = [(subject_type, subject_id)]

        now_sql = "NOW()" if self._is_postgresql else "datetime('now')"

        with self._engine.connect() as conn:
            while queue:
                curr_type, curr_id = queue.pop(0)
                if (curr_type, curr_id) in visited:
                    continue
                visited.add((curr_type, curr_id))

                query = text(f"""
                    SELECT object_type, object_id
                    FROM rebac_tuples
                    WHERE subject_type = :subj_type
                      AND subject_id = :subj_id
                      AND relation IN ('member-of', 'member', 'belongs-to')
                      AND zone_id = :zone_id
                      AND (expires_at IS NULL OR expires_at > {now_sql})
                """)
                result = conn.execute(
                    query,
                    {"subj_type": curr_type, "subj_id": curr_id, "zone_id": zone_id},
                )

                for row in result:
                    group = (row.object_type, row.object_id)
                    if group not in groups:
                        groups.add(group)
                        queue.append(group)

        return groups

    # ------------------------------------------------------------------
    # Administrative helpers
    # ------------------------------------------------------------------

    def rebuild_closure(self, zone_id: str) -> int:
        """Rebuild the Leopard transitive closure for *zone_id*.

        Returns:
            Number of closure entries created.

        Raises:
            RuntimeError: If Leopard index is not enabled.
        """
        if not self._leopard:
            raise RuntimeError("Leopard index is not enabled")
        return self._leopard.rebuild_closure_for_zone(zone_id)

    def invalidate_cache(self, zone_id: str | None = None) -> None:
        """Invalidate Leopard in-memory cache.

        Args:
            zone_id: If provided, only invalidate for this zone.
        """
        if not self._leopard:
            return
        if zone_id:
            self._leopard.invalidate_cache_for_zone(zone_id)
        else:
            self._leopard.clear_cache()

    # ------------------------------------------------------------------
    # Write-path helpers (called by TupleWriter / manager)
    # ------------------------------------------------------------------

    def on_membership_add(
        self,
        subject_type: str,
        subject_id: str,
        object_type: str,
        object_id: str,
        zone_id: str,
    ) -> None:
        """Update closure after a membership relation is added."""
        if not self._leopard:
            return
        try:
            entries = self._leopard.update_closure_on_membership_add(
                subject_type=subject_type,
                subject_id=subject_id,
                group_type=object_type,
                group_id=object_id,
                zone_id=zone_id,
            )
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "[LEOPARD] Updated closure for %s:%s -> %s:%s: %s entries",
                    subject_type,
                    subject_id,
                    object_type,
                    object_id,
                    entries,
                )
        except (OperationalError, ProgrammingError) as e:
            logger.warning("[LEOPARD] Failed to update closure: %s", e)

    def on_membership_remove(
        self,
        subject_type: str,
        subject_id: str,
        object_type: str,
        object_id: str,
        zone_id: str,
    ) -> None:
        """Update closure after a membership relation is removed."""
        if not self._leopard:
            return
        try:
            entries = self._leopard.update_closure_on_membership_remove(
                subject_type=subject_type,
                subject_id=subject_id,
                group_type=object_type,
                group_id=object_id,
                zone_id=zone_id,
            )
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "[LEOPARD] Removed closure for %s:%s -> %s:%s: %s entries",
                    subject_type,
                    subject_id,
                    object_type,
                    object_id,
                    entries,
                )
        except (OperationalError, ProgrammingError) as e:
            logger.warning("[LEOPARD] Failed to update closure on delete: %s", e)

    def add_synthetic_tuples(
        self,
        tuples: list[dict[str, Any]],
        subject_type: str,
        subject_id: str,
        zone_id: str,
    ) -> None:
        """Append synthetic membership tuples from transitive closure to *tuples*.

        Used by ``_fetch_tuples_for_rust`` so the Rust graph engine sees
        direct membership edges for every transitive group membership.
        """
        if not self._leopard:
            return

        transitive_groups = self._leopard.get_transitive_groups(
            member_type=subject_type,
            member_id=subject_id,
            zone_id=zone_id,
        )
        if not transitive_groups:
            return

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[LEOPARD] Adding %d synthetic membership tuples for %s:%s",
                len(transitive_groups),
                subject_type,
                subject_id,
            )
        for group_type, group_id in transitive_groups:
            tuples.append(
                {
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "subject_relation": None,
                    "relation": "member",
                    "object_type": group_type,
                    "object_id": group_id,
                }
            )

    @property
    def enabled(self) -> bool:
        """Return True if the Leopard index is enabled."""
        return self._leopard is not None

    @property
    def index(self) -> LeopardIndex | None:
        """Return the underlying LeopardIndex (or None)."""
        return self._leopard
