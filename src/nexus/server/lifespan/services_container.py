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
    service_coordinator: Any = None  # ServiceLifecycleCoordinator

    # --- Process table (kernel process lifecycle) -------------------------
    process_table: Any = None

    # --- System services (from nexus_fs._system_services) ----------------
    brick_lifecycle_manager: Any = None
    brick_reconciler: Any = None
    eviction_manager: Any = None
    write_observer: Any = None
    zone_lifecycle: Any = None
    pipe_manager: Any = None  # DT_PIPE manager — kernel-internal primitive (§4.2)

    # --- Issue #2195, #2360: Scheduler (from SystemServices) ----
    scheduler_service: "SchedulerProtocol | None" = None

    # --- DT_PIPE consumers (Issue #810) -----------------------------------
    zoekt_pipe_consumer: Any = None

    # --- Brick services container ----------------------------------------
    brick_services: Any = None  # The whole BrickServices dataclass

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
        _sys = getattr(nx, "_system_services", None) if nx else None
        _brk = getattr(nx, "_brick_services", None) if nx else None

        _coord = getattr(nx, "_service_coordinator", None) if nx else None

        return cls(
            # Core / kernel
            nexus_fs=nx,
            database_url=getattr(app.state, "database_url", None),
            record_store=getattr(app.state, "record_store", None),
            zone_id=getattr(app.state, "zone_id", None),
            # Process table
            process_table=getattr(app.state, "process_table", None),
            # Coordinator
            service_coordinator=_coord,
            # Configuration
            deployment_profile=getattr(app.state, "deployment_profile", "full"),
            deployment_mode=getattr(app.state, "deployment_mode", "standalone"),
            enabled_bricks=getattr(app.state, "enabled_bricks", frozenset()),
            profile_tuning=getattr(app.state, "profile_tuning", None),
            thread_pool_size=getattr(app.state, "thread_pool_size", 40),
            # System services
            brick_lifecycle_manager=(
                getattr(_sys, "brick_lifecycle_manager", None) if _sys else None
            ),
            brick_reconciler=(getattr(_sys, "brick_reconciler", None) if _sys else None),
            eviction_manager=(getattr(_sys, "eviction_manager", None) if _sys else None),
            write_observer=(getattr(_sys, "write_observer", None) if _sys else None),
            zone_lifecycle=(getattr(_sys, "zone_lifecycle", None) if _sys else None),
            pipe_manager=(getattr(nx, "_pipe_manager", None) if nx else None),
            # Issue #810: DT_PIPE Zoekt consumer
            zoekt_pipe_consumer=(getattr(_brk, "zoekt_pipe_consumer", None) if _brk else None),
            # Issue #2195: Scheduler
            scheduler_service=(getattr(_sys, "scheduler_service", None) if _sys else None),
            # Brick services
            brick_services=_brk,
            # NexusFS internals
            session_factory=getattr(nx, "SessionLocal", None) if nx else None,
            sql_engine=getattr(nx, "_sql_engine", None) if nx else None,
            entity_registry=(getattr(nx, "_entity_registry", None) if nx else None),
            permission_enforcer=(getattr(nx, "_permission_enforcer", None) if nx else None),
            rebac_manager=(getattr(nx, "_rebac_manager", None) if nx else None),
            event_bus=getattr(nx, "_event_bus", None) if nx else None,
            coordination_client=(getattr(nx, "_coordination_client", None) if nx else None),
            workflow_engine=(getattr(nx, "workflow_engine", None) if nx else None),
            snapshot_service=(getattr(nx, "_snapshot_service", None) if nx else None),
            namespace_manager=(getattr(nx, "_namespace_manager", None) if nx else None),
            nexus_config=getattr(nx, "config", None) if nx else None,
            observability_subsystem=(
                getattr(_sys, "observability_subsystem", None) if _sys else None
            ),
            # From app.state (set by server init)
            observability_registry=getattr(app.state, "observability_registry", None),
        )
