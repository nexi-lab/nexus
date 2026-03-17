"""Service-layer protocol interfaces (Issue #1383).

Convention (Issue #1291):
- All protocols use @runtime_checkable for test-time isinstance() checks.
- Do NOT use isinstance(obj, Protocol) in production hot paths.
- All data classes use @dataclass(frozen=True, slots=True).
- TYPE_CHECKING for all nexus.* imports (except zero-dep leaf modules).

These protocols define domain-service contracts.  They live in contracts/
because they are tier-neutral interface definitions consumed by bricks,
services, and the kernel alike.

Storage Affinity (per data-storage-matrix.md):
    - NamespaceManagerProtocol -> RecordStore + CacheStore (ReBAC views)
    - SchedulerProtocol      -> CacheStore or RecordStore (work queue)

References:
    - docs/architecture/data-storage-matrix.md
    - Issue #1383: Define 6 kernel protocol interfaces
"""

from nexus.contracts.filesystem.filesystem_abc import NexusFilesystemABC
from nexus.contracts.protocols.auth import APIKeyCreatorProtocol
from nexus.contracts.protocols.brick_lifecycle import (
    BrickReconcileOutcome,
    LifecycleManagerProtocol,
    ReconcileContext,
    ReconcilerProtocol,
)
from nexus.contracts.protocols.chunked_upload import ChunkedUploadProtocol
from nexus.contracts.protocols.entity_registry import EntityRegistryProtocol
from nexus.contracts.protocols.file_reader import FileReaderProtocol
from nexus.contracts.protocols.mcp import MCPProtocol
from nexus.contracts.protocols.mount import MountProtocol, ProgressCallback
from nexus.contracts.protocols.mount_persist import MountPersistProtocol
from nexus.contracts.protocols.namespace_manager import NamespaceManagerProtocol
from nexus.contracts.protocols.oauth import OAuthProtocol
from nexus.contracts.protocols.operation_log import OperationLogProtocol
from nexus.contracts.protocols.parse import ParseProtocol
from nexus.contracts.protocols.payment import PaymentProtocol
from nexus.contracts.protocols.permission import PermissionProtocol
from nexus.contracts.protocols.permission_enforcer import PermissionEnforcerProtocol
from nexus.contracts.protocols.rebac import ReBACBrickProtocol
from nexus.contracts.protocols.sandbox import SandboxProtocol
from nexus.contracts.protocols.scheduler import AgentRequest, SchedulerProtocol
from nexus.contracts.protocols.search import SearchBrickProtocol, SearchProtocol
from nexus.contracts.protocols.service_lifecycle import HotSwappable, PersistentService
from nexus.contracts.protocols.share_link import ShareLinkProtocol
from nexus.contracts.protocols.sync import SyncContext, SyncResult, SyncServiceProtocol
from nexus.contracts.protocols.sync_job import SyncJobProtocol
from nexus.contracts.protocols.time_travel import TimeTravelProtocol
from nexus.contracts.protocols.version import VersionProtocol
from nexus.contracts.protocols.watch import WatchProtocol
from nexus.contracts.protocols.workflow_dispatch import WorkflowDispatchProtocol
from nexus.contracts.protocols.workspace_manager import WorkspaceManagerProtocol
from nexus.contracts.protocols.write_back import WriteBackProtocol

__all__ = [
    "APIKeyCreatorProtocol",
    "AgentRequest",
    "BrickReconcileOutcome",
    "ChunkedUploadProtocol",
    "EntityRegistryProtocol",
    "FileReaderProtocol",
    "HotSwappable",
    "LifecycleManagerProtocol",
    "MCPProtocol",
    "MountPersistProtocol",
    "MountProtocol",
    "NamespaceManagerProtocol",
    "NexusFilesystemABC",
    "OAuthProtocol",
    "OperationLogProtocol",
    "ParseProtocol",
    "PaymentProtocol",
    "PersistentService",
    "PermissionEnforcerProtocol",
    "PermissionProtocol",
    "ProgressCallback",
    "ReBACBrickProtocol",
    "ReconcileContext",
    "ReconcilerProtocol",
    "SandboxProtocol",
    "SchedulerProtocol",
    "SearchBrickProtocol",
    "SearchProtocol",
    "ShareLinkProtocol",
    "SyncContext",
    "SyncJobProtocol",
    "SyncResult",
    "SyncServiceProtocol",
    "TimeTravelProtocol",
    "VersionProtocol",
    "WatchProtocol",
    "WorkflowDispatchProtocol",
    "WorkspaceManagerProtocol",
    "WriteBackProtocol",
]
