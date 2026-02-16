"""Persistent namespace view protocol and data model (Issue #1265).

Defines the PersistentViewStore protocol for L3 cache — persists constructed
namespace views for instant restoration on agent reconnection. Sits between
in-memory mount table (L2) and full ReBAC rebuild.

Inspired by Twizzler OS's persistent FOT views (ATC 2020).

Storage Affinity: **RecordStore** — relational upsert keyed on subject+zone.

Architecture:
    Request → dcache L1 (O(1)) → mount table L2 (O(log m)) → L3 persistent (1-3ms)
    → ReBAC rebuild (5-50ms)

Key design:
    - Keyed on (subject_type, subject_id, zone_id) — not agent-specific
    - Invalidated via zone revision bucket comparison
    - Upsert semantics — one row per subject, self-cleaning
    - Optional: persistent_store=None disables L3 (graceful degradation)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class PersistentView:
    """A persisted namespace view for a subject.

    Contains the mount paths and metadata needed to restore a subject's
    namespace without querying ReBAC.

    Attributes:
        subject_type: Subject type (e.g., "user", "agent")
        subject_id: Subject identifier
        zone_id: Zone for multi-zone isolation (None → "default")
        mount_paths: Sorted tuple of mount prefix strings (immutable)
        grants_hash: 16-char SHA-256 hex digest of sorted grants
        revision_bucket: Zone revision bucket when view was built
        created_at: When this view was first created
    """

    subject_type: str
    subject_id: str
    zone_id: str | None
    mount_paths: tuple[str, ...]
    grants_hash: str
    revision_bucket: int
    created_at: datetime


class PersistentViewStore(Protocol):
    """Protocol for persistent namespace view storage (L3 cache).

    Implementations must provide save/load/delete for namespace views.
    All methods are synchronous (called from sync NamespaceManager context).

    Structural subtyping — no need to inherit from this protocol.
    """

    def save_view(
        self,
        subject_type: str,
        subject_id: str,
        zone_id: str | None,
        mount_paths: list[str],
        grants_hash: str,
        revision_bucket: int,
    ) -> None:
        """Persist a namespace view (upsert semantics).

        If a view already exists for (subject_type, subject_id, zone_id),
        it is replaced with the new data.

        Args:
            subject_type: Subject type (e.g., "user", "agent")
            subject_id: Subject identifier
            zone_id: Zone ID (None → stored as "default")
            mount_paths: Sorted list of mount prefix strings
            grants_hash: 16-char SHA-256 hex digest of sorted grants
            revision_bucket: Zone revision bucket when view was built
        """
        ...

    def load_view(
        self,
        subject_type: str,
        subject_id: str,
        zone_id: str | None,
    ) -> PersistentView | None:
        """Load a persisted namespace view.

        Args:
            subject_type: Subject type
            subject_id: Subject identifier
            zone_id: Zone ID (None → looked up as "default")

        Returns:
            PersistentView if found, None if no view exists for this subject/zone.
        """
        ...

    def delete_views(
        self,
        subject_type: str,
        subject_id: str,
    ) -> int:
        """Delete all persisted views for a subject (all zones).

        Args:
            subject_type: Subject type
            subject_id: Subject identifier

        Returns:
            Number of views deleted.
        """
        ...

    def delete_all_views(self) -> int:
        """Delete all persisted views across all subjects and zones.

        Used by invalidate_all() for full cache reset.

        Returns:
            Number of views deleted.
        """
        ...
