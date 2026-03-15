"""Factory helpers — _safe_create, _make_gate, brick registration."""

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Profile gating helper (Issue #2193: DRY for _on() closure)
# ---------------------------------------------------------------------------


def _make_gate(brick_on: Callable[[str], bool] | None) -> Callable[[str], bool]:
    """Create a profile gate closure.

    Replaces the repeated ``_on()`` inner function pattern across tier modules.

    Args:
        brick_on: Callable ``(name: str) -> bool`` for profile-based gating.
            When None, returns a gate that enables everything.

    Returns:
        A ``(name: str) -> bool`` callable for gating service creation.
    """
    if brick_on is None:
        return lambda _name: True
    return brick_on


# ---------------------------------------------------------------------------
# Issue #1704: Register factory-created bricks with lifecycle manager
# ---------------------------------------------------------------------------

# Bricks registered with lifecycle manager.
# Only stateful bricks (implementing start/stop/health_check) where
# mount/unmount actually does something. Stateless bricks go to _FACTORY_SKIP.
# (name, protocol_name, depends_on)
_FACTORY_BRICKS: list[tuple[str, str, tuple[str, ...]]] = [
    # Currently empty — all stateful bricks are registered separately:
    # - workflow_engine: via _WorkflowLifecycleAdapter in _register_factory_bricks()
    # - cache: via _register_late_bricks() in create_nexus_fs()
    # - search: in server/lifespan/search.py
]

# Entries intentionally NOT registered with lifecycle manager.
# CI test ``test_all_brick_dict_keys_accounted_for`` will fail if a new
# key appears in ``_boot_independent_bricks()`` without being added here or
# to ``_FACTORY_BRICKS``.
_FACTORY_SKIP: frozenset[str] = frozenset(
    {
        # --- Not from nexus/bricks/ (services/infrastructure) ---
        "event_bus",  # nexus/services/event_subsystem/
        "lock_manager",  # nexus/raft/
        "chunked_upload_service",  # nexus/services/upload/
        "task_queue_service",  # nexus/system_services/lifecycle/
        "wallet_provisioner",  # nexus/factory/wallet
        "version_service",  # nexus/services/versioning/
        "zoekt_pipe_consumer",  # nexus/factory/zoekt_pipe_consumer
        # --- Stateless bricks (no start/stop — unmount is cosmetic) ---
        "manifest_resolver",  # nexus/bricks/context_manifest/
        "manifest_metrics",  # nexus/bricks/context_manifest/
        "snapshot_service",  # nexus/bricks/snapshot/
        "ipc_storage_driver",  # nexus/bricks/ipc/
        "ipc_provisioner",  # nexus/bricks/ipc/
        "delegation_service",  # nexus/bricks/delegation/
        "reputation_service",  # nexus/bricks/reputation/
        "api_key_creator",  # nexus/bricks/auth/
        "tool_namespace_middleware",  # nexus/bricks/mcp/
        "agent_event_log",  # nexus/bricks/sandbox/
        "rebac_circuit_breaker",  # nexus/bricks/rebac/
        "memory_permission",  # nexus/bricks/rebac/
        "governance_anomaly_service",  # nexus/bricks/governance/
        "governance_collusion_service",  # nexus/bricks/governance/
        "governance_graph_service",  # nexus/bricks/governance/
        "governance_response_service",  # nexus/bricks/governance/
        # --- Always None at boot ---
        "skill_service",  # wired later via NexusFS gateway adapters
        "skill_package_service",  # wired later via NexusFS gateway adapters
    }
)

_LATE_BRICKS: list[tuple[str, str, tuple[str, ...]]] = [
    ("cache", "CacheProtocol", ()),
]


def _register_late_bricks(manager: Any, brick_dict: dict[str, Any]) -> None:
    """Register stateful bricks created in create_nexus_fs()."""
    for name, protocol, depends_on in _LATE_BRICKS:
        instance = brick_dict.get(name)
        if instance is not None:
            manager.register(name, instance, protocol_name=protocol, depends_on=depends_on)


def _register_factory_bricks(
    manager: Any,
    brick_dict: dict[str, Any],
) -> None:
    """Register Tier 2 bricks from ``_boot_independent_bricks()`` with the lifecycle manager.

    Skips infrastructure entries (event_bus, lock_manager, etc.) and None values.
    WorkflowEngine gets a thin adapter since its startup API differs.
    """
    from nexus.factory.adapters import _WorkflowLifecycleAdapter

    for name, protocol, depends_on in _FACTORY_BRICKS:
        instance = brick_dict.get(name)
        if instance is not None:
            manager.register(name, instance, protocol_name=protocol, depends_on=depends_on)

    # WorkflowEngine needs adapter (startup() != start())
    wf = brick_dict.get("workflow_engine")
    if wf is not None:
        manager.register(
            "workflow_engine",
            _WorkflowLifecycleAdapter(wf),
            protocol_name="WorkflowProtocol",
        )


def _safe_create(
    name: str,
    factory_fn: Callable[[], Any],
    brick_on: Callable[[str], bool],
    tier: str = "BRICK",
    severity: str = "debug",
) -> Any:
    """Create a service with profile gating + error handling.

    Severity levels (Issue #2193):
        ``"debug"``   — Brick-tier default.  Log at DEBUG on failure, return None.
        ``"warning"`` — System-tier degradable.  Log at WARNING on failure, return None.
        ``"critical"``— System-tier critical.  Log at CRITICAL and raise ``BootError``.

    Returns the created service, or None if gated or on non-critical failure.
    """
    if not brick_on(name):
        logger.debug("[BOOT:%s] %s disabled by profile", tier, name)
        return None
    try:
        result = factory_fn()
        logger.debug("[BOOT:%s] %s created", tier, name)
        return result
    except Exception as exc:
        if severity == "critical":
            from nexus.contracts.exceptions import BootError

            logger.critical("[BOOT:%s] %s FATAL: %s", tier, name, exc)
            raise BootError(f"{name}: {exc}", tier=tier) from exc
        getattr(logger, severity)("[BOOT:%s] %s unavailable: %s", tier, name, exc)
        return None
