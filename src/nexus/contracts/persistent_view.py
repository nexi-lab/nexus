"""Persistent namespace view data type (Issue #1265).

Tier-0 data contract for L3 persistent namespace views.
Used by storage layer (PostgresPersistentViewStore) and service layer
(PersistentViewStore protocol).

Split from nexus.contracts.protocols.persistent_view to allow storage
tier to import without a services→storage tier violation.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class PersistentView:
    """A persisted namespace view for a subject.

    Contains the mount paths and metadata needed to restore a subject's
    namespace without querying ReBAC.

    Attributes:
        subject_type: Subject type (e.g., "user", "agent")
        subject_id: Subject identifier
        zone_id: Zone for multi-zone isolation (None → "root")
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
