"""Kernel protocol interfaces for the Nexus architecture.

VFSRouterProtocol lives here — it is a kernel concern (mount table + path routing).
Service-layer protocols (EventLogProtocol, PermissionProtocol, etc.) live in
nexus.services.protocols/ per the Four Pillars architecture.

References:
    - docs/architecture/data-storage-matrix.md
    - Issue #1383: Define 6 kernel protocol interfaces
"""

from nexus.core.protocols.caching import CachingConnectorContract
from nexus.core.protocols.connector import (
    BatchContentProtocol,
    ConnectorProtocol,
    ContentStoreProtocol,
    DirectoryListingProtocol,
    DirectoryOpsProtocol,
    OAuthCapableProtocol,
    PassthroughProtocol,
    StreamingProtocol,
)
from nexus.core.protocols.content_service import ContentServiceProtocol
from nexus.core.protocols.describable import Describable
from nexus.core.protocols.revision_service import RevisionServiceProtocol
from nexus.core.protocols.vfs_core import VFSCoreProtocol
from nexus.core.protocols.vfs_router import MountInfo, ResolvedPath, VFSRouterProtocol

__all__ = [
    "BatchContentProtocol",
    "CachingConnectorContract",
    "ConnectorProtocol",
    "ContentServiceProtocol",
    "ContentStoreProtocol",
    "Describable",
    "DirectoryListingProtocol",
    "DirectoryOpsProtocol",
    "MountInfo",
    "OAuthCapableProtocol",
    "PassthroughProtocol",
    "ResolvedPath",
    "RevisionServiceProtocol",
    "StreamingProtocol",
    "VFSCoreProtocol",
    "VFSRouterProtocol",
]
