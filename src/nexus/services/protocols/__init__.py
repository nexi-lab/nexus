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
    - EventLogProtocol       → RecordStore (append-only BRIN audit log)
    - HookEngineProtocol     → CacheStore (ephemeral hook registration)
    - NamespaceManagerProtocol → RecordStore + CacheStore (ReBAC views)
    - SchedulerProtocol      → CacheStore or RecordStore (work queue)

References:
    - docs/architecture/data-storage-matrix.md
    - Issue #1383: Define 6 kernel protocol interfaces
"""

from nexus.services.protocols.agent_registry import AgentInfo, AgentRegistryProtocol
from nexus.services.protocols.context_manifest import ContextManifestProtocol
from nexus.services.protocols.event_log import EventId, EventLogProtocol, KernelEvent
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
from nexus.services.protocols.namespace_manager import NamespaceManagerProtocol, NamespaceMount
from nexus.services.protocols.scheduler import AgentRequest, SchedulerProtocol
from nexus.services.protocols.search import SearchBrickProtocol

__all__ = [
    "AgentInfo",
    "AgentRegistryProtocol",
    "AgentRequest",
    "ContextManifestProtocol",
    "EventId",
    "EventLogProtocol",
    "HookContext",
    "HookEngineProtocol",
    "HookId",
    "HookResult",
    "HookSpec",
    "KernelEvent",
    "NamespaceManagerProtocol",
    "NamespaceMount",
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
    "SchedulerProtocol",
    "SearchBrickProtocol",
]
