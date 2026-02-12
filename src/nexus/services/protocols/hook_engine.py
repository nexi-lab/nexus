"""Hook engine service protocol (Nexus Lego Architecture, Issue #1383).

Defines the contract for lifecycle hook registration and execution.
Existing implementation: ``nexus.plugins.hooks.PluginHooks`` (partially async).

Storage Affinity: **CacheStore** — ephemeral hook registrations (in-memory / Dragonfly).

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md Part 2
    - docs/architecture/data-storage-matrix.md (Four Pillars)
    - Issue #1383: Define 6 kernel protocol interfaces
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Phase constants — open ``str`` values, not an enum, so bricks can define
# custom phases without modifying this module.
# ---------------------------------------------------------------------------
PRE_READ: str = "pre_read"
POST_READ: str = "post_read"
PRE_WRITE: str = "pre_write"
POST_WRITE: str = "post_write"
PRE_DELETE: str = "pre_delete"
POST_DELETE: str = "post_delete"
PRE_MKDIR: str = "pre_mkdir"
POST_MKDIR: str = "post_mkdir"
PRE_COPY: str = "pre_copy"
POST_COPY: str = "post_copy"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HookId:
    """Opaque identifier for a registered hook.

    Attributes:
        id: Unique hook registration identifier.
    """

    id: str


@dataclass(frozen=True, slots=True)
class HookSpec:
    """Specification for registering a hook.

    Attributes:
        phase: Lifecycle phase (e.g. ``PRE_WRITE``).
        handler_name: Human-readable name for logging / debugging.
        priority: Execution priority (higher = executed first).  Default 0.
    """

    phase: str
    handler_name: str
    priority: int = 0


@dataclass(frozen=True, slots=True)
class HookContext:
    """Context passed to hook handlers when fired.

    Attributes:
        phase: The lifecycle phase being executed.
        path: Virtual path involved (if applicable).
        zone_id: Zone/organization ID.
        agent_id: Agent performing the operation (if applicable).
        payload: Arbitrary phase-specific data.
    """

    phase: str
    path: str | None
    zone_id: str | None
    agent_id: str | None
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class HookResult:
    """Result returned by a hook handler.

    Attributes:
        proceed: Whether the operation should continue.
        modified_context: Optional replacement context dict for downstream hooks.
        error: Error message if the hook vetoed the operation.
    """

    proceed: bool
    modified_context: dict[str, Any] | None
    error: str | None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class HookEngineProtocol(Protocol):
    """Service contract for lifecycle hook registration and execution.

    All methods are async.  The existing ``PluginHooks`` class conforms
    with minor adaptation (method names and data types differ slightly).
    """

    async def register_hook(
        self,
        spec: HookSpec,
        handler: Callable[..., Awaitable[HookResult]],
    ) -> HookId: ...

    async def unregister_hook(self, hook_id: HookId) -> bool: ...

    async def fire(self, phase: str, context: HookContext) -> HookResult: ...
