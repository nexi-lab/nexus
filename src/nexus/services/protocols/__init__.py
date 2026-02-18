"""Service-layer protocol interfaces (Issue #1383).

Convention (Issue #1291):
- All protocols use @runtime_checkable for test-time isinstance() checks.
- Do NOT use isinstance(obj, Protocol) in production hot paths.
- All data classes use @dataclass(frozen=True, slots=True).
- TYPE_CHECKING for all nexus.* imports (except zero-dep leaf modules).

These protocols define domain-service contracts. They are NOT kernel primitives —
they live in services/ because their implementations depend on one or more of the
Four Pillars (Metastore, RecordStore, ObjectStore, CacheStore) rather than being
storage abstractions themselves.

Only VFSRouterProtocol remains in core/protocols/ as it is a kernel concern
(virtual path routing is a fundamental filesystem operation).

Storage Affinity (per data-storage-matrix.md):
    - AgentRegistryProtocol  → RecordStore (relational agent identity)
    - HookEngineProtocol     → CacheStore (ephemeral hook registration)
    - NamespaceManagerProtocol → RecordStore + CacheStore (ReBAC views)
    - SchedulerProtocol      → CacheStore or RecordStore (work queue)

References:
    - docs/architecture/data-storage-matrix.md
    - Issue #1383: Define 6 kernel protocol interfaces
"""

from nexus.rebac.namespace_manager import NamespaceMount
from nexus.services.event_log.protocol import EventLogConfig, EventLogProtocol
from nexus.services.governance.protocols import AnomalyDetectorProtocol
from nexus.services.protocols.agent_registry import AgentInfo, AgentRegistryProtocol
from nexus.services.protocols.auth import APIKeyCreatorProtocol
from nexus.services.protocols.chunked_upload import ChunkedUploadProtocol
from nexus.services.protocols.delegation import DelegationProtocol
from nexus.services.protocols.hook_engine import (
    POST_COPY,
    POST_DELETE,
    POST_MKDIR,
    POST_READ,
    POST_WRITE,
    PRE_COPY,
    PRE_DELETE,
    PRE_MKDIR,
    PRE_READ,
    PRE_WRITE,
    HookContext,
    HookEngineProtocol,
    HookId,
    HookResult,
    HookSpec,
)
from nexus.services.protocols.llm import LLMServiceProtocol
from nexus.services.protocols.llm_provider import LLMProviderProtocol
from nexus.services.protocols.lock import LockProtocol
from nexus.services.protocols.mcp import MCPProtocol
from nexus.services.protocols.memory import MemoryProtocol
from nexus.services.protocols.mount import MountProtocol, ProgressCallback
from nexus.services.protocols.mount_core import MountCoreProtocol
from nexus.services.protocols.mount_persist import MountPersistProtocol
from nexus.services.protocols.namespace_manager import NamespaceManagerProtocol
from nexus.services.protocols.oauth import OAuthProtocol
from nexus.services.protocols.operation_log import OperationLogProtocol
from nexus.services.protocols.parse import ParseProtocol
from nexus.services.protocols.payment import PaymentProtocol
from nexus.services.protocols.permission import PermissionProtocol
from nexus.services.protocols.plugin import PluginProtocol
from nexus.services.protocols.rebac import ReBACBrickProtocol
from nexus.services.protocols.reputation import ReputationProtocol
from nexus.services.protocols.scheduler import AgentRequest, SchedulerProtocol
from nexus.services.protocols.search import SearchBrickProtocol, SearchProtocol
from nexus.services.protocols.share_link import ShareLinkProtocol
from nexus.services.protocols.skills import SkillsProtocol
from nexus.services.protocols.sync import SyncContext, SyncResult, SyncServiceProtocol
from nexus.services.protocols.sync_job import SyncJobProtocol
from nexus.services.protocols.task_queue import TaskQueueProtocol
from nexus.services.protocols.trajectory import TrajectoryProtocol
from nexus.services.protocols.version import VersionProtocol
from nexus.services.protocols.watch import WatchProtocol
from nexus.services.protocols.write_back import WriteBackProtocol
from nexus.workflows.protocol import (
    MetadataStoreProtocol,
    NexusOperationsProtocol,
    WorkflowLLMProtocol,
    WorkflowProtocol,
)

__all__ = [
    "APIKeyCreatorProtocol",
    "AgentInfo",
    "AgentRegistryProtocol",
    "AgentRequest",
    "AnomalyDetectorProtocol",
    "ChunkedUploadProtocol",
    "DelegationProtocol",
    "EventLogConfig",
    "EventLogProtocol",
    "HookContext",
    "HookEngineProtocol",
    "HookId",
    "HookResult",
    "HookSpec",
    "LLMProviderProtocol",
    "LLMServiceProtocol",
    "LockProtocol",
    "MCPProtocol",
    "MemoryProtocol",
    "MetadataStoreProtocol",
    "MountCoreProtocol",
    "MountPersistProtocol",
    "MountProtocol",
    "NamespaceManagerProtocol",
    "NamespaceMount",
    "NexusOperationsProtocol",
    "OAuthProtocol",
    "OperationLogProtocol",
    "POST_COPY",
    "POST_DELETE",
    "POST_MKDIR",
    "POST_READ",
    "POST_WRITE",
    "PRE_COPY",
    "PRE_DELETE",
    "PRE_MKDIR",
    "PRE_READ",
    "PRE_WRITE",
    "ParseProtocol",
    "PaymentProtocol",
    "PermissionProtocol",
    "PluginProtocol",
    "ProgressCallback",
    "ReBACBrickProtocol",
    "ReputationProtocol",
    "SchedulerProtocol",
    "SearchBrickProtocol",
    "SearchProtocol",
    "ShareLinkProtocol",
    "SkillsProtocol",
    "SyncContext",
    "SyncJobProtocol",
    "SyncResult",
    "SyncServiceProtocol",
    "TaskQueueProtocol",
    "TrajectoryProtocol",
    "VersionProtocol",
    "WatchProtocol",
    "WorkflowLLMProtocol",
    "WorkflowProtocol",
    "WriteBackProtocol",
]
