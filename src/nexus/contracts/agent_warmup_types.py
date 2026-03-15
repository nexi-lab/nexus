"""Agent warmup phase types (Issue #2172).

Pure value objects for structured agent initialization before accepting work.
Zero runtime dependencies — only stdlib imports.

WarmupStep defines a named initialization step with timeout and required/optional
semantics. WarmupResult captures the outcome of a warmup attempt. WarmupContext
provides typed dependency injection for step functions.

Design:
    - Server-orchestrated: warmup runs on the server side
    - Sequential execution: steps run in order with per-step timeouts
    - Required steps gate transition to CONNECTED
    - Optional steps log failures but continue
"""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any


@dataclass(frozen=True, slots=True)
class WarmupStep:
    """A single initialization step in the agent warmup sequence.

    Attributes:
        name: Step identifier (e.g. "load_credentials", "mount_namespace").
        timeout: Maximum time allowed for this step. Exceeding triggers
            asyncio.TimeoutError.
        required: If True, step failure aborts warmup and agent stays
            in UNKNOWN state. If False, failure is logged and warmup
            continues to the next step.
    """

    name: str
    timeout: timedelta = field(default_factory=lambda: timedelta(seconds=30))
    required: bool = True


@dataclass(frozen=True, slots=True)
class WarmupResult:
    """Outcome of an agent warmup attempt.

    Immutable snapshot capturing exactly what happened during warmup:
    which steps completed, which were skipped (optional failures),
    which step caused failure (if any), and total duration.

    Attributes:
        success: True if all required steps passed and agent transitioned
            to CONNECTED.
        agent_id: The agent that was warmed up.
        steps_completed: Names of steps that completed successfully, in order.
        steps_skipped: Names of optional steps that failed (logged, not fatal).
        failed_step: Name of the required step that failed, or None on success.
        error: Human-readable error message if warmup failed.
        duration_ms: Total wallclock time of the warmup in milliseconds.
    """

    success: bool
    agent_id: str
    steps_completed: tuple[str, ...] = ()
    steps_skipped: tuple[str, ...] = ()
    failed_step: str | None = None
    error: str | None = None
    duration_ms: float = 0.0


@dataclass(frozen=True)
class WarmupContext:
    """Typed dependency bag for warmup step functions.

    Each step function receives this context and accesses only the
    dependencies it needs. Optional dependencies are None when the
    corresponding service is not available.

    Follows the LifespanServices pattern from server/lifespan/services_container.py.

    Attributes:
        agent_id: The agent being warmed up.
        agent_record: Immutable snapshot of the agent at warmup start.
        agent_registry: AgentRegistry for state queries.
        namespace_manager: NamespaceManager for mount resolution (optional).
        enabled_bricks: Set of brick names enabled in this deployment.
        cache_store: CacheStoreABC for cache warming (optional).
        mcp_config: MCP server configuration (optional).
    """

    agent_id: str
    agent_record: Any  # AgentRecord (TYPE_CHECKING import avoided for zero-dep)
    agent_registry: Any  # AgentRegistry
    namespace_manager: Any | None = None
    enabled_bricks: frozenset[str] = field(default_factory=frozenset)
    cache_store: Any | None = None
    mcp_config: dict[str, Any] | None = None


# Type alias for warmup step functions is defined in the service module
# to avoid importing async types into a pure-data contracts module.

# Standard warmup sequence for common agent types (Issue #2172).
STANDARD_WARMUP: tuple[WarmupStep, ...] = (
    WarmupStep("load_credentials", timeout=timedelta(seconds=5)),
    WarmupStep("mount_namespace", timeout=timedelta(seconds=10)),
    WarmupStep("verify_bricks", timeout=timedelta(seconds=10)),
    WarmupStep("warm_caches", timeout=timedelta(seconds=15), required=False),
    WarmupStep("connect_mcp", timeout=timedelta(seconds=10), required=False),
)
