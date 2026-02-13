"""Kernel protocol interfaces for the Nexus architecture.

VFSRouterProtocol and ContextManifestProtocol live here — they are kernel concerns.
EventLogProtocol is also a kernel concern (durable event persistence).

Service-layer protocols (EventLogProtocol, etc.) live in nexus.services/
per the Four Pillars architecture (data-storage-matrix.md).

References:
    - docs/architecture/data-storage-matrix.md
    - Issue #1383: Define 6 kernel protocol interfaces
    - Issue #1397: Event Log WAL
    - Issue #1341: Context manifest protocol
"""

from nexus.core.protocols.context_manifest import ContextManifestProtocol
from nexus.core.protocols.event_log import EventLogConfig, EventLogProtocol
from nexus.core.protocols.vfs_router import MountInfo, ResolvedPath, VFSRouterProtocol

__all__ = [
    "ContextManifestProtocol",
    "EventLogConfig",
    "EventLogProtocol",
    "MountInfo",
    "ResolvedPath",
    "VFSRouterProtocol",
]
