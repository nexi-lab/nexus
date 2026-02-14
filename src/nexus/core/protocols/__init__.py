"""Kernel protocol interfaces for the Nexus architecture.

VFSRouterProtocol lives here — it is a kernel concern (mount table + path routing).
Service-layer protocols (EventLogProtocol, ContextManifestProtocol, etc.) live in
nexus.services.protocols/ per the Four Pillars architecture.

References:
    - docs/architecture/data-storage-matrix.md
    - Issue #1383: Define 6 kernel protocol interfaces
"""

from nexus.core.protocols.vfs_router import MountInfo, ResolvedPath, VFSRouterProtocol

__all__ = [
    "MountInfo",
    "ResolvedPath",
    "VFSRouterProtocol",
]
