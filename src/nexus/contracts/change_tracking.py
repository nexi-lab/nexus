"""ChangeTrackingProtocol — structural protocol for metastore change feeds.

Separated from MetastoreABC because change tracking is a Raft replication
concern, not an inode CRUD concern.  Only Metastore implementations that
support replication change feeds (e.g. Raft ring buffer) implement this.

MetastoreABC stays pure CRUD (get/put/delete/exists/list/close).
WatchCacheManager depends on this Protocol, not MetastoreABC.

Issue #2065.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from nexus.contracts.metadata_change import MetadataChange


@runtime_checkable
class ChangeTrackingProtocol(Protocol):
    """Structural protocol for metastore implementations with change feeds.

    Only implement this on stores that actually track changes (e.g. Raft).
    Non-Raft stores don't need a no-op stub.
    """

    def drain_changes(self, since_revision: int = 0) -> list[MetadataChange]:
        """Drain metadata changes since the given revision.

        Returns revision-ordered list of changes and clears them from
        the internal buffer.

        Args:
            since_revision: Only return changes with revision > this value.

        Returns:
            List of MetadataChange events, ordered by revision.
        """
        ...
