"""Kernel protocol interfaces for the Nexus architecture.

VFSCoreProtocol lives here — it's a kernel concern (core VFS operations).

ConnectorProtocol family also lives here — these are kernel boundary contracts
for storage backend abstraction.

Non-kernel protocols have been moved to their correct tier locations:
- Service protocols (EntityRegistry, PermissionEnforcer, ReBACManager,
  WorkspaceManager) → nexus.contracts.protocols/
- Cross-tier contracts (Describable, WirableFS) → nexus.contracts/
- ContentServiceProtocol → deleted (zero consumers)
- ReBACManagerProtocol → merged into ReBACBrickProtocol (DRY)
- VFSRouterProtocol / ResolvedPath / MountInfo → deleted (zero consumers).
  The Rust kernel's `VFSRouter` is the SSOT; Python callers go through
  `kernel.route()` / `kernel.get_mount_points()` directly.

References:
    - docs/architecture/data-storage-matrix.md
    - NEXUS-LEGO-ARCHITECTURE.md §2.2
    - Issue #1383: Define 6 kernel protocol interfaces
    - Issue #2359: Move non-kernel protocols out of core/protocols/
"""

from nexus.contracts.cache_store import CacheStoreABC, NullCacheStore
from nexus.core.protocols.caching import CacheConfigContract
from nexus.contracts.backend_features import BackendFeature
from nexus.core.protocols.connector import (
    BatchContentProtocol,
    CapabilityAwareProtocol,
    ConnectorProtocol,
    ContentStoreProtocol,
    DirectoryListingProtocol,
    DirectoryOpsProtocol,
    OAuthCapableProtocol,
    PathDeleteProtocol,
    SearchableConnector,
    SignedUrlProtocol,
    StreamingProtocol,
)
from nexus.core.protocols.vfs_core import VFSCoreProtocol

__all__ = [
    "BatchContentProtocol",
    "CacheConfigContract",
    "CacheStoreABC",
    "CapabilityAwareProtocol",
    "BackendFeature",
    "ConnectorProtocol",
    "ContentStoreProtocol",
    "DirectoryListingProtocol",
    "DirectoryOpsProtocol",
    "NullCacheStore",
    "OAuthCapableProtocol",
    "PathDeleteProtocol",
    "SearchableConnector",
    "SignedUrlProtocol",
    "StreamingProtocol",
    "VFSCoreProtocol",
]
