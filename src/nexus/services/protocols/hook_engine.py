"""Hook engine service protocol (Issue #1383, #1257, #2064).

Defines the contract for lifecycle hook registration and execution.
Existing implementation: ``nexus.plugins.hooks.PluginHooks`` (partially async).

Storage Affinity: **CacheStore** — ephemeral hook registrations (in-memory / Dragonfly).

Issue #2064: Kubernetes-style mutating/validating phases with failure policies.
- Mutating hooks run first (sequential), can modify context and veto.
- Validating hooks run second (sequential), can veto but NOT modify.
- FailurePolicy per-hook: FAIL (abort on error) or IGNORE (log and continue).

References:
    - docs/architecture/KERNEL-ARCHITECTURE.md §3
    - docs/architecture/data-storage-matrix.md (Four Pillars)
    - Issue #1383: Define 6 kernel protocol interfaces
    - Issue #1257: Hook engine per-agent scoping and verified execution
    - Issue #2064: Mutating/validating phases with failure policies
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
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

# Brick lifecycle phases (Issue #1704)
PRE_MOUNT: str = "pre_mount"
POST_MOUNT: str = "post_mount"
PRE_UNMOUNT: str = "pre_unmount"
POST_UNMOUNT: str = "post_unmount"

# Artifact lifecycle phases (Issue #1861)
POST_ARTIFACT_CREATE: str = "post_artifact_create"
POST_ARTIFACT_UPDATE: str = "post_artifact_update"

# ---------------------------------------------------------------------------
# Enums (Issue #2064)
# ---------------------------------------------------------------------------


class FailurePolicy(StrEnum):
    """Per-hook failure policy (Kubernetes admission controller pattern).

    Determines what happens when a hook handler errors or times out.

    - ``FAIL``:   Error/timeout aborts the operation (proceed=False).
    - ``IGNORE``: Error/timeout is logged; operation continues (proceed=True).

    Default: ``IGNORE`` (backward compatible with pre-#2064 behavior).
    """

    FAIL = "fail"
    IGNORE = "ignore"


class HookPhaseType(StrEnum):
    """Determines execution ordering within a lifecycle phase (Issue #2064).

    Modeled after Kubernetes admission controllers:

    - ``MUTATING``:   Runs first. Can modify context AND veto.
                      Sequential execution in priority order.
    - ``VALIDATING``: Runs second. Can veto but CANNOT modify context.
                      Sequential execution; sees the final mutated context.

    When ``HookSpec.phase_type`` is not set (``None``), the engine derives
    the type from the phase string (``pre_*`` → ``MUTATING``, ``post_*``
    hooks always run in the POST phase regardless).
    """

    MUTATING = "mutating"
    VALIDATING = "validating"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HookCapabilities:
    """Declared capabilities for a hook handler (Issue #1257, #2064).

    Verified at registration time and enforced at execution time.
    Inspired by eBPF's verifier — handlers declare what they're
    allowed to do, and the engine enforces it.

    Attributes:
        can_veto: Whether the handler is allowed to return ``proceed=False``.
        can_modify_context: Whether the handler may return ``modified_context``.
        max_timeout_ms: Maximum execution time in milliseconds.
        failure_policy: What happens on handler error/timeout (Issue #2064).
    """

    can_veto: bool = True
    can_modify_context: bool = True
    max_timeout_ms: int = 5000
    failure_policy: FailurePolicy = FailurePolicy.IGNORE

    def __post_init__(self) -> None:
        if self.max_timeout_ms <= 0:
            raise ValueError("max_timeout_ms must be positive")


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
        agent_scope: If set, hook fires only for this agent.  ``None`` = global.
        capabilities: Declared capabilities for verified execution.
        phase_type: Execution ordering within PRE phases (Issue #2064).
            ``None`` = derive from phase string (pre_* → MUTATING).
    """

    phase: str
    handler_name: str
    priority: int = 0
    agent_scope: str | None = None
    capabilities: HookCapabilities = field(default_factory=HookCapabilities)
    phase_type: HookPhaseType | None = None


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

    Invariants enforced by ``__post_init__``:
      - ``proceed=False`` requires ``error`` and forbids ``modified_context``.
      - ``proceed=True`` forbids ``error``.

    Attributes:
        proceed: Whether the operation should continue.
        modified_context: Optional replacement context dict for downstream hooks.
        error: Error message if the hook vetoed the operation.
        failure_mode: Why the operation was blocked (Issue #2064).
            ``None`` = not blocked or handler returned normally.
            ``"veto"`` = handler explicitly returned proceed=False.
            ``"timeout"`` = handler timed out and failure_policy=FAIL.
            ``"error"`` = handler raised and failure_policy=FAIL.
    """

    proceed: bool
    modified_context: dict[str, Any] | None
    error: str | None
    failure_mode: str | None = None

    def __post_init__(self) -> None:
        if not self.proceed and self.modified_context is not None:
            raise ValueError("Vetoed HookResult (proceed=False) must not have modified_context")
        if not self.proceed and self.error is None:
            raise ValueError("Vetoed HookResult (proceed=False) must include an error message")
        if self.proceed and self.error is not None:
            raise ValueError("Proceeding HookResult (proceed=True) must not have an error")


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
