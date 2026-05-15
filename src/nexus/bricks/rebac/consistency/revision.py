"""Revision helpers — Version tokens and zone revision lookups.

Extracts the consistency-related methods from ReBACManager
into standalone functions. These support Zanzibar-style consistency
tokens (zookies) for snapshot reads and bounded staleness.

Usage:
    from nexus.bricks.rebac.consistency.revision import (
        increment_version_token,
        get_zone_revision_for_grant,
    )

    store = MetastoreVersionStore(nexus_fs)
    token = increment_version_token(store, zone_id="org_acme")
    revision = get_zone_revision_for_grant(store, zone_id="org_acme")

Related: Issue #1459 (decomposition), P0-1 (consistency levels)
Issue #191: Migrated from SQLAlchemy ORM to MetastoreABC. The store
later moved off the metastore entirely — it now writes through public
VFS syscalls (sys_write/sys_read) under ``/__sys__/rebac/versions/``.
"""

import logging

from nexus.bricks.rebac.consistency.metastore_version_store import MetastoreVersionStore
from nexus.contracts.constants import ROOT_ZONE_ID

logger = logging.getLogger(__name__)


def increment_version_token(
    version_store: MetastoreVersionStore,
    zone_id: str = ROOT_ZONE_ID,
) -> str:
    """Atomically increment and return the version token for a zone.

    Uses MetastoreABC (redb) for persistence. Atomic at the
    Metastore level: redb ACID for single-node, Raft consensus
    serializes writes for multi-node.

    Args:
        version_store: MetastoreVersionStore instance
        zone_id: Zone ID to increment version for

    Returns:
        Monotonic version token string (e.g., "v123")
    """
    version = version_store.increment_version(zone_id)
    return f"v{version}"


def get_zone_revision_for_grant(version_store: MetastoreVersionStore, zone_id: str) -> int:
    """Get current zone revision for consistency during expansion.

    This prevents the "new enemy" problem: files created after the grant
    revision are not automatically included (user must explicitly include
    future files or re-grant).

    Args:
        version_store: MetastoreVersionStore instance
        zone_id: Zone ID

    Returns:
        Current revision number (0 if not found)
    """
    return version_store.get_version(zone_id)
