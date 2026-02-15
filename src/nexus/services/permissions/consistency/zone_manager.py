"""Zone Manager — Zone isolation enforcement for ReBAC.

Extracts the zone isolation validation logic from ZoneAwareReBACManager
into a standalone, composable helper. This enforces Zanzibar-style zone
boundaries: tuples can only link entities within the same zone unless
the relation is in CROSS_ZONE_ALLOWED_RELATIONS.

Usage:
    from nexus.services.permissions.consistency.zone_manager import (
        ZoneManager, ZoneIsolationError,
    )

    manager = ZoneManager(enforce=True)
    zone_id, subj, obj, is_cross = manager.validate_write_zones(
        zone_id="org_acme",
        subject_zone_id=None,
        object_zone_id=None,
        relation="editor",
    )

Related: Issue #1459 (decomposition), Issue #773 (zone isolation)
"""

from __future__ import annotations

import logging

from nexus.core.rebac import CROSS_ZONE_ALLOWED_RELATIONS

logger = logging.getLogger(__name__)


class ZoneIsolationError(Exception):
    """Raised when attempting cross-zone operations.

    Attributes:
        subject_zone: The subject's zone ID
        object_zone: The object's zone ID
    """

    def __init__(self, message: str, subject_zone: str | None, object_zone: str | None):
        super().__init__(message)
        self.subject_zone = subject_zone
        self.object_zone = object_zone


class ZoneManager:
    """Zone isolation enforcement helper.

    Validates that permission tuples respect zone boundaries.
    Cross-zone shares are allowed only for relations in
    ``CROSS_ZONE_ALLOWED_RELATIONS``.

    Args:
        enforce: Whether to enforce zone isolation (kill-switch for migration)
    """

    def __init__(self, enforce: bool = True) -> None:
        self.enforce = enforce

    def _resolve_zone_defaults(
        self,
        zone_id: str | None,
        subject_zone_id: str | None,
        object_zone_id: str | None,
    ) -> tuple[str, str, str]:
        """Resolve None/empty zone IDs to defaults.

        Returns:
            Tuple of (zone_id, subject_zone_id, object_zone_id)
        """
        if not zone_id:
            zone_id = "default"
        subject_zone_id = subject_zone_id or zone_id
        object_zone_id = object_zone_id or zone_id
        return zone_id, subject_zone_id, object_zone_id

    def is_cross_zone_readable(self, relation: str) -> bool:
        """Check if a relation allows cross-zone read access.

        Cross-zone shares (e.g., shared-viewer, shared-editor) are stored
        in the resource owner's zone but should be visible when checking
        from the recipient's zone. Wildcard access is always cross-zone
        readable.

        Args:
            relation: The relation type to check

        Returns:
            True if the relation allows cross-zone reads
        """
        return relation in CROSS_ZONE_ALLOWED_RELATIONS

    def validate_write_zones(
        self,
        zone_id: str | None,
        subject_zone_id: str | None,
        object_zone_id: str | None,
        relation: str,
    ) -> tuple[str, str, str, bool]:
        """Validate and resolve zone IDs for a write operation.

        Applies zone defaults, checks cross-zone isolation, and determines
        the effective zone_id for storage. When ``enforce=False``, defaults
        are still resolved but isolation checks are skipped.

        Args:
            zone_id: Primary zone ID (defaults to "default" if falsy)
            subject_zone_id: Subject's zone (defaults to zone_id)
            object_zone_id: Object's zone (defaults to zone_id)
            relation: The relation being written

        Returns:
            Tuple of (effective_zone_id, subject_zone_id, object_zone_id, is_cross_zone)

        Raises:
            ZoneIsolationError: If cross-zone write is not allowed for this relation
                (only when ``enforce=True``)
        """
        zone_id, subject_zone_id, object_zone_id = self._resolve_zone_defaults(
            zone_id, subject_zone_id, object_zone_id
        )

        is_cross_zone = subject_zone_id != object_zone_id

        # Kill-switch: skip enforcement but still resolve defaults
        if not self.enforce:
            return zone_id, subject_zone_id, object_zone_id, is_cross_zone

        is_cross_zone_allowed = relation in CROSS_ZONE_ALLOWED_RELATIONS

        if is_cross_zone:
            if is_cross_zone_allowed:
                # Cross-zone shares stored in object's zone (resource owner)
                zone_id = object_zone_id
                if logger.isEnabledFor(logging.INFO):
                    logger.info(
                        "Cross-zone share: %s -> %s (relation=%s, stored in zone=%s)",
                        subject_zone_id,
                        object_zone_id,
                        relation,
                        zone_id,
                    )
            else:
                raise ZoneIsolationError(
                    f"Cannot create cross-zone relationship: "
                    f"subject in {subject_zone_id}, object in {object_zone_id}",
                    subject_zone_id,
                    object_zone_id,
                )

        if subject_zone_id != zone_id and not is_cross_zone_allowed:
            raise ZoneIsolationError(
                f"Subject zone {subject_zone_id} does not match tuple zone {zone_id}",
                subject_zone_id,
                zone_id,
            )

        return zone_id, subject_zone_id, object_zone_id, is_cross_zone
