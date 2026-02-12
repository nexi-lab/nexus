"""Kernel protocol interfaces for the Nexus architecture.

Only VFSRouterProtocol lives here — it is a kernel concern (virtual path routing).

The remaining 5 service-layer protocols live in nexus.services.protocols/
per the Four Pillars architecture (data-storage-matrix.md).

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
