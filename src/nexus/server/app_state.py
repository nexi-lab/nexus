"""Typed reference for all app.state fields and initialization helper.

Issue #2135: Consolidate ad-hoc app.state field initialization into a
single dataclass + helper so lifespan modules never reach into NexusFS
private attributes.

``NexusAppState`` is **not** used as a runtime container (FastAPI uses
``SimpleNamespace``).  It serves as:

1. IDE autocomplete / type reference
2. Input for ``init_app_state()`` which guarantees every field is initialized
3. Documentation for the 60+ fields currently set across create_app()
"""

import logging
from dataclasses import MISSING, dataclass, field, fields
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


@dataclass
class NexusAppState:
    """Type reference for all app.state fields.

    Not used as runtime container (FastAPI uses SimpleNamespace).
    Used by ``init_app_state()`` to guarantee all fields are initialized.
    """

    # === Core (set by create_app) ===
    nexus_fs: Any = None
    database_url: str | None = None
    api_key: str | None = None
    auth_provider: Any = None
    data_dir: str | None = None
    brick_container: Any = None

    # === Deployment config ===
    deployment_profile: str = "full"
    deployment_mode: str = "standalone"
    enabled_bricks: frozenset[str] = field(default_factory=frozenset)
    profile_tuning: Any = None
    features_info: Any = None

    # === Flattened from NexusFS (replaces private attr access) ===
    rebac_manager: Any = None
    entity_registry: Any = None
    namespace_manager: Any = None
    event_bus: Any = None
    write_observer: Any = None
    permission_enforcer: Any = None
    record_store: Any = None

    # === From ServiceRegistry ===
    observability_subsystem: Any = None
    eviction_manager: Any = None

    # === Database sessions ===
    async_session_factory: Any = None
    session_factory: Any = None
    read_session_factory: Any = None
    async_read_session_factory: Any = None

    # === Observability ===
    observability_registry: Any = None

    # === Services (initialized to None, set during lifespan) ===
    agent_warmup_service: Any = None
    async_rebac_manager: Any = None
    key_service: Any = None
    credential_service: Any = None
    scheduler_service: Any = None
    task_runner: Any = None
    task_manager_service: Any = None
    task_write_hook: Any = None
    task_dispatch_consumer: Any = None
    workflow_engine: Any = None
    workflow_dispatch: Any = None
    sandbox_auth_service: Any = None
    agent_event_log: Any = None
    transactional_snapshot_service: Any = None

    # === Realtime ===
    subscription_manager: Any = None
    search_daemon: Any = None
    search_daemon_enabled: bool = False
    directory_grant_expander: Any = None
    cache_brick: Any = None
    websocket_manager: Any = None
    reactive_subscription_manager: Any = None
    exporter_registry: Any = None

    # === Permissions ===
    rebac_circuit_breaker: Any = None

    # === Governance (Issue #2129) ===
    governance_anomaly_service: Any = None
    governance_collusion_service: Any = None
    governance_graph_service: Any = None
    governance_response_service: Any = None

    # === Services (brick-sourced) ===
    delegation_service: Any = None
    chunked_upload_service: Any = None

    # === IPC ===
    ipc_nexus_fs: Any = None
    ipc_provisioner: Any = None
    ipc_sweeper: Any = None

    # === gRPC server (#1249) ===
    grpc_server: Any = None

    # === Approvals brick (Issue #3790) ===
    # ApprovalsStack instance from nexus.bricks.approvals.bootstrap.
    # When NEXUS_APPROVALS_ENABLED is unset (default), .service and .gate are None.
    approvals_stack: Any = None
    # PolicyGate instance OR None when approvals are disabled. The MCP egress
    # hook (Task 18) and hub zone-access hook (Task 19) read this and treat
    # `None` as "approvals disabled" by contract.
    policy_gate: Any = None

    # === Exposed methods ===
    exposed_methods: dict[str, Any] = field(default_factory=dict)

    # === Thread pool / timeout ===
    thread_pool_size: int = 40
    operation_timeout: float = 30.0

    # === Rate limiter ===
    limiter: Any = None

    # === Health probes (Issue #2168) ===
    startup_tracker: Any = None

    # === Internal background tasks (prefixed with _) ===
    # These are managed by lifespan and not part of public API


def init_app_state(app: "FastAPI", nexus_fs: Any = None, **overrides: Any) -> None:
    """Initialize all app.state fields from NexusAppState defaults.

    Replaces 60+ lines of ``app.state.x = None`` in ``create_app()``.
    Flattens NexusFS internal attrs onto ``app.state`` so lifespan
    modules never reach into private attributes.

    Args:
        app: FastAPI application instance.
        nexus_fs: NexusFS instance (may be None for testing).
        **overrides: Additional key=value pairs to set on app.state.
    """
    # Set all NexusAppState defaults
    for f in fields(NexusAppState):
        if not hasattr(app.state, f.name):
            if f.default is not MISSING:
                default: Any = f.default
            elif f.default_factory is not MISSING:
                factory = f.default_factory
                default = factory()
            else:
                default = None
            setattr(app.state, f.name, default)

    # Set core param
    app.state.nexus_fs = nexus_fs

    # Apply caller overrides
    for k, v in overrides.items():
        setattr(app.state, k, v)

    # Flatten NexusFS internals onto app.state
    if nexus_fs is not None:
        _flatten_nexus_fs(app, nexus_fs)


def _flatten_nexus_fs(app: "FastAPI", nexus_fs: Any) -> None:
    """Flatten NexusFS internals onto app.state for typed access.

    All services accessed via ServiceRegistry.
    """
    # Direct NexusFS attrs
    app.state.permission_enforcer = (
        nexus_fs.service("permission_enforcer") if hasattr(nexus_fs, "service") else None
    )

    # Helper: safe service() call (handles mocks without service())
    def _svc(name: str) -> Any:
        svc_fn = getattr(nexus_fs, "service", None)
        return svc_fn(name) if svc_fn is not None else None

    # All from ServiceRegistry
    app.state.event_bus = _svc("event_bus")
    app.state.write_observer = _svc("write_observer")
    app.state.observability_subsystem = _svc("observability_subsystem")
    app.state.eviction_manager = _svc("eviction_manager")
