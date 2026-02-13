"""Kernel protocol interfaces for the Nexus architecture.

Only VFSRouterProtocol lives here — it is a kernel concern (virtual path routing).

Service-layer protocols (EventLogProtocol, etc.) live in nexus.services/
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
