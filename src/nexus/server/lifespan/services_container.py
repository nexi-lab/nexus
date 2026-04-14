"""Typed service container for lifespan modules (Issue #2135).

Replaces untyped ``getattr()`` / ``hasattr()`` reflection in lifespan
startup/shutdown with a single frozen extraction performed once at
server startup.  All ``from_app()`` extraction logic lives here —
lifespan modules consume typed attributes instead of probing
``app.state`` and NexusFS internals.

Design decisions:
    - Fields are ``Any | None`` to avoid circular imports.
    - ``from_app()`` centralises every ``getattr()`` call so no lifespan
      module ever touches NexusFS private attributes directly.
    - The container is **not** frozen because some startup modules
      may need to cache additional references after extraction.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

    from nexus.contracts.protocols.scheduler import SchedulerProtocol


@dataclass(slots=True)
class LifespanServices:
    """Typed snapshot of factory-produced services for lifespan modules.

    Populated once via ``from_app()`` at the beginning of the lifespan
    context manager.  Lifespan sub-modules receive this object instead
    of reaching into ``app.state`` / NexusFS internals with reflection.

    Writes to ``app.state`` (for router access) still use ``app``
    directly — this container is read-only by convention.
    """

    # --- Core / kernel ---------------------------------------------------
    nexus_fs: Any = None
    database_url: str | None = None
    record_store: Any = None
    zone_id: str | None = None

    # --- Configuration ---------------------------------------------------
    deployment_profile: str = "full"
    deployment_mode: str = "standalone"
    enabled_bricks: frozenset[str] = field(default_factory=frozenset)
    profile_tuning: Any = None
    thread_pool_size: int = 40

    # --- Coordinator (post-bootstrap service registration) ---------------
    service_coordinator: Any = None  # NexusFS (delegates to Rust kernel)

    # --- Process table (kernel process lifecycle) -------------------------
    agent_registry: Any = None

    # --- System services (from ServiceRegistry) ----------------
    eviction_manager: Any = None
    write_observer: Any = None
    zone_lifecycle: Any = None

    # --- Scheduler (from ServiceRegistry) ----
    scheduler_service: "SchedulerProtocol | None" = None

    # --- Issue #3193: delivery worker + event signal -------------------------
    delivery_worker: Any = None
    event_signal: Any = None

    # --- DT_PIPE consumers (Issue #810) -----------------------------------
    zoekt_pipe_consumer: Any = None
    task_dispatch_consumer: Any = None  # Task Manager DT_PIPE consumer

    # --- NexusFS internals (extracted once, never re-probed) --------------
    session_factory: Any = None  # NexusFS.SessionLocal
    sql_engine: Any = None
    entity_registry: Any = None
    permission_enforcer: Any = None
    rebac_manager: Any = None
    event_bus: Any = None
    coordination_client: Any = None
    workflow_engine: Any = None
    snapshot_service: Any = None
    namespace_manager: Any = None
    nexus_config: Any = None
    observability_subsystem: Any = None

    # --- From app.state (set by server init, not factory) ----------------
    observability_registry: Any = None

    @classmethod
    def from_app(cls, app: "FastAPI") -> "LifespanServices":
        """Extract all factory-produced services into a typed container.

        This method is the **single place** where ``getattr()`` is used to
        reach into ``app.state`` and NexusFS internals.  After this call,
        lifespan modules access services via typed attributes.
        """
        nx = getattr(app.state, "nexus_fs", None)
        _coord = getattr(nx, "service_coordinator", None) if nx else None

        # Helper: nx.service() with None safety (also handles test mocks without service())
        def _svc(name: str) -> Any:
            if nx is None:
                return None
            svc_fn = getattr(nx, "service", None)
            return svc_fn(name) if svc_fn is not None else None

        return cls(
            # Core / kernel
            nexus_fs=nx,
            database_url=getattr(app.state, "database_url", None),
            record_store=getattr(app.state, "record_store", None),
            zone_id=getattr(app.state, "zone_id", None),
            agent_registry=(
                _svc("agent_registry") or getattr(nx, "_agent_registry", None)
                if nx
                else getattr(app.state, "agent_registry", None)
            ),
            service_coordinator=_coord,
            # Configuration
            deployment_profile=getattr(app.state, "deployment_profile", "full"),
            deployment_mode=getattr(app.state, "deployment_mode", "standalone"),
            enabled_bricks=getattr(app.state, "enabled_bricks", frozenset()),
            profile_tuning=getattr(app.state, "profile_tuning", None),
            thread_pool_size=getattr(app.state, "thread_pool_size", 40),
            # All services from ServiceRegistry
            delivery_worker=_svc("delivery_worker"),
            event_signal=None,
            eviction_manager=_svc("eviction_manager"),
            write_observer=_svc("write_observer"),
            zone_lifecycle=_svc("zone_lifecycle"),
            zoekt_pipe_consumer=_svc("zoekt_pipe_consumer"),
            task_dispatch_consumer=_svc("task_dispatch_consumer"),
            scheduler_service=_svc("scheduler_service"),
            # NexusFS internals
            session_factory=getattr(nx, "SessionLocal", None) if nx else None,
            sql_engine=getattr(nx, "_sql_engine", None) if nx else None,
            entity_registry=_svc("entity_registry"),
            permission_enforcer=(_svc("permission_enforcer") if nx else None),
            rebac_manager=_svc("rebac_manager"),
            event_bus=getattr(nx, "_event_bus", None) if nx else None,
            coordination_client=None,
            workflow_engine=(
                (nx.service("workflow_engine") if hasattr(nx, "service") else None)
                or getattr(nx, "workflow_engine", None)
                if nx
                else None
            ),
            snapshot_service=_svc("snapshot_service"),
            namespace_manager=_svc("async_namespace_manager"),
            nexus_config=getattr(nx, "config", None) if nx else None,
            observability_subsystem=_svc("observability_subsystem"),
            # From app.state (set by server init)
            observability_registry=getattr(app.state, "observability_registry", None),
        )
