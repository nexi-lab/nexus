"""Kernel protocol interfaces for the Nexus Lego Architecture (Issue #1383).

This package defines the foundational contracts that all future brick
implementations program against.  Each protocol uses ``@runtime_checkable``
and async methods.

Protocols:
    - ``AgentRegistryProtocol`` — agent identity and lifecycle management
    - ``NamespaceManagerProtocol`` — per-subject namespace visibility
    - ``VFSRouterProtocol`` — virtual path routing to storage backends
    - ``HookEngineProtocol`` — lifecycle hook registration and execution
    - ``SchedulerProtocol`` — agent work-request scheduling

Note:
    ``EventLogProtocol`` lives in ``nexus.services.event_log`` (not kernel).
    EventLog is a service-layer concern, like journald vs. syscalls.

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md Part 2
    - Issue #1383: Define kernel protocol interfaces
"""

from nexus.core.protocols.agent_registry import AgentInfo, AgentRegistryProtocol
from nexus.core.protocols.hook_engine import (
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
from nexus.core.protocols.namespace_manager import NamespaceManagerProtocol, NamespaceMount
from nexus.core.protocols.scheduler import AgentRequest, SchedulerProtocol
from nexus.core.protocols.vfs_router import MountInfo, ResolvedPath, VFSRouterProtocol

__all__ = [
    "AgentInfo",
    "AgentRegistryProtocol",
    "AgentRequest",
    "HookContext",
    "HookEngineProtocol",
    "HookId",
    "HookResult",
    "HookSpec",
    "NamespaceManagerProtocol",
    "MountInfo",
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
    "ResolvedPath",
    "SchedulerProtocol",
    "VFSRouterProtocol",
]
