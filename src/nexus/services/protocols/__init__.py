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
    - NamespaceManagerProtocol → RecordStore + CacheStore (ReBAC views)
    - SchedulerProtocol      → CacheStore or RecordStore (work queue)

References:
    - docs/architecture/data-storage-matrix.md
    - Issue #1383: Define 6 kernel protocol interfaces
"""

import importlib as _il

from nexus.contracts.workflow_types import (
    MetadataStoreProtocol,
    NexusOperationsProtocol,
)
from nexus.lib.rpc_decorator import rpc_expose
from nexus.services.protocols.adaptive_k import AdaptiveKProtocol
from nexus.services.protocols.agent_registry import AgentInfo, AgentRegistryProtocol
from nexus.services.protocols.auth import APIKeyCreatorProtocol
from nexus.services.protocols.brick_lifecycle import (
    BrickReconcileOutcome,
    LifecycleManagerProtocol,
    ReconcileContext,
    ReconcilerProtocol,
)
from nexus.services.protocols.chunked_upload import ChunkedUploadProtocol
from nexus.services.protocols.entity_registry import EntityRegistryProtocol
from nexus.services.protocols.file_reader import FileReaderProtocol
from nexus.services.protocols.filesystem import NexusFilesystem
from nexus.services.protocols.llm import LLMServiceProtocol
from nexus.services.protocols.llm_provider import LLMProviderProtocol
from nexus.services.protocols.lock import LockProtocol
from nexus.services.protocols.mcp import MCPProtocol
from nexus.services.protocols.memory import MemoryProtocol
from nexus.services.protocols.memory_deps import (
    MemoryEntityRegistryProtocol,
    MemoryPermissionProtocol,
)
from nexus.services.protocols.mount import MountProtocol, ProgressCallback
from nexus.services.protocols.mount_core import MountCoreProtocol
from nexus.services.protocols.mount_persist import MountPersistProtocol
from nexus.services.protocols.namespace_manager import NamespaceManagerProtocol
from nexus.services.protocols.oauth import OAuthProtocol
from nexus.services.protocols.operation_log import OperationLogProtocol
from nexus.services.protocols.parse import ParseProtocol
from nexus.services.protocols.payment import PaymentProtocol
from nexus.services.protocols.permission import PermissionProtocol
from nexus.services.protocols.permission_enforcer import PermissionEnforcerProtocol
from nexus.services.protocols.rebac import ReBACBrickProtocol
from nexus.services.protocols.sandbox import SandboxProtocol
from nexus.services.protocols.scheduler import AgentRequest, SchedulerProtocol
from nexus.services.protocols.search import SearchBrickProtocol, SearchProtocol
from nexus.services.protocols.share_link import ShareLinkProtocol
from nexus.services.protocols.skill_deps import SkillFilesystemProtocol, SkillPermissionProtocol
from nexus.services.protocols.skill_doc import SkillDocGenerator, generate_skill_md
from nexus.services.protocols.skills import SkillsProtocol
from nexus.services.protocols.sync import SyncContext, SyncResult, SyncServiceProtocol
from nexus.services.protocols.sync_job import SyncJobProtocol
from nexus.services.protocols.task_queue import TaskQueueProtocol
from nexus.services.protocols.time_travel import TimeTravelProtocol
from nexus.services.protocols.version import VersionProtocol
from nexus.services.protocols.watch import WatchProtocol
from nexus.services.protocols.workflow_dispatch import WorkflowDispatchProtocol
from nexus.services.protocols.workspace_manager import WorkspaceManagerProtocol
from nexus.services.protocols.write_back import WriteBackProtocol
from nexus.system_services.event_subsystem.log.protocol import EventLogConfig, EventLogProtocol

# Brick import via importlib to avoid services→bricks tier violation
NamespaceMount = _il.import_module("nexus.bricks.rebac.namespace_manager").NamespaceMount

__all__ = [
    "APIKeyCreatorProtocol",
    "AdaptiveKProtocol",
    "BrickReconcileOutcome",
    "AgentInfo",
    "AgentRegistryProtocol",
    "AgentRequest",
    "ChunkedUploadProtocol",
    "EntityRegistryProtocol",
    "EventLogConfig",
    "FileReaderProtocol",
    "EventLogProtocol",
    "LifecycleManagerProtocol",
    "LLMProviderProtocol",
    "LLMServiceProtocol",
    "LockProtocol",
    "MCPProtocol",
    "MemoryEntityRegistryProtocol",
    "MemoryPermissionProtocol",
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
    "ParseProtocol",
    "PaymentProtocol",
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
    "NexusFilesystem",
    "SkillDocGenerator",
    "SkillFilesystemProtocol",
    "SkillPermissionProtocol",
    "SkillsProtocol",
    "SyncContext",
    "generate_skill_md",
    "rpc_expose",
    "SyncJobProtocol",
    "SyncResult",
    "SyncServiceProtocol",
    "TaskQueueProtocol",
    "TimeTravelProtocol",
    "VersionProtocol",
    "WatchProtocol",
    "WorkflowDispatchProtocol",
    "WorkspaceManagerProtocol",
    "WriteBackProtocol",
]
