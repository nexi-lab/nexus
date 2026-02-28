"""Factory helpers — _safe_create, _make_gate, _resolve_tasks_db_path, brick registration."""

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

_FACTORY_BRICKS: list[tuple[str, str]] = [
    ("manifest_resolver", "ManifestProtocol"),
    ("chunked_upload_service", "ChunkedUploadProtocol"),
    ("snapshot_service", "SnapshotProtocol"),
    ("task_queue_service", "TaskQueueProtocol"),
    ("ipc_vfs_driver", "IPCProtocol"),
    ("wallet_provisioner", "WalletProtocol"),
    ("delegation_service", "DelegationProtocol"),
    ("reputation_service", "ReputationProtocol"),
    ("version_service", "VersionProtocol"),  # Issue #2034: moved from kernel
]

# Entries intentionally NOT registered with lifecycle manager.
# CI test ``test_all_brick_dict_keys_accounted_for`` will fail if a new
# key appears in ``_boot_independent_bricks()`` without being added here or
# to ``_FACTORY_BRICKS``.
_FACTORY_SKIP: frozenset[str] = frozenset(
    {
        "event_bus",  # infrastructure, not a brick
        "lock_manager",  # infrastructure, not a brick
        "api_key_creator",  # class reference, not instance
        "tool_namespace_middleware",  # stateless middleware, no lifecycle
        "manifest_metrics",  # observability helper, not a brick
        "ipc_storage_driver",  # internal to ipc_vfs_driver
        "ipc_provisioner",  # provisioning helper, not a brick
        "agent_event_log",  # event log, not a lifecycle brick
        "rebac_circuit_breaker",  # Issue #2034: passive resilience wrapper, no lifecycle
        "memory_permission",  # singleton component for Memory brick (Issue #2177)
        "governance_anomaly_service",  # governance brick, no lifecycle (Issue #2129)
        "governance_collusion_service",  # governance brick, no lifecycle (Issue #2129)
        "governance_graph_service",  # governance brick, no lifecycle (Issue #2129)
        "governance_response_service",  # governance brick, no lifecycle (Issue #2129)
        "zoekt_pipe_consumer",  # DT_PIPE consumer, no lifecycle (Issue #810)
    }
)


def _register_factory_bricks(
    manager: Any,
    brick_dict: dict[str, Any],
) -> None:
    """Register Tier 2 bricks from ``_boot_independent_bricks()`` with the lifecycle manager.

    Skips infrastructure entries (event_bus, lock_manager, etc.) and None values.
    WorkflowEngine gets a thin adapter since its startup API differs.
    """
    from nexus.factory.adapters import _WorkflowLifecycleAdapter

    for name, protocol in _FACTORY_BRICKS:
        instance = brick_dict.get(name)
        if instance is not None:
            manager.register(name, instance, protocol_name=protocol)

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


def _resolve_tasks_db_path(backend: Any) -> str:
    """Resolve the fjall database path for TaskQueueService.

    Priority:
    1. NEXUS_TASKS_DB_PATH environment variable
    2. NEXUS_DATA_DIR/tasks-db
    3. backend.root_path/../tasks-db (alongside backend storage)
    4. .nexus-data/tasks-db (fallback)
    """
    import os

    env_path = os.environ.get("NEXUS_TASKS_DB_PATH")
    if env_path:
        return env_path

    data_dir = os.environ.get("NEXUS_DATA_DIR")
    if data_dir:
        return os.path.join(data_dir, "tasks-db")

    root_path = getattr(backend, "root_path", None)
    if root_path is not None:
        return os.path.join(str(root_path), "tasks-db")

    return os.path.join(".nexus-data", "tasks-db")
