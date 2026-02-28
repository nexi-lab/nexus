"""Distributed infrastructure — event bus, event log, lock manager, workflow engine."""

import logging
import os
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from nexus.bricks.workflows.protocol import WorkflowProtocol
    from nexus.core.config import DistributedConfig
    from nexus.core.metastore import MetastoreABC

logger = logging.getLogger(__name__)


def _create_event_log_wal() -> Any:
    """Create WAL EventLog for durable event persistence (Issue #2175).

    Uses env vars for configuration (same as server lifespan):
    - NEXUS_WAL_DIR: WAL directory (default: .nexus-data/wal)
    - NEXUS_WAL_SYNC_MODE: fsync mode (default: "every")
    - NEXUS_WAL_SEGMENT_SIZE: segment size in bytes (default: 4MB)

    Returns EventLogProtocol instance or None if Rust WAL unavailable.
    """
    try:
        from pathlib import Path

        from nexus.system_services.event_subsystem.log.factory import create_event_log
        from nexus.system_services.event_subsystem.log.protocol import EventLogConfig

        wal_dir = os.getenv("NEXUS_WAL_DIR", ".nexus-data/wal")
        sync_mode_raw = os.getenv("NEXUS_WAL_SYNC_MODE", "every")
        sync_mode: Literal["every", "none"] = "every" if sync_mode_raw != "none" else "none"
        segment_size = int(os.getenv("NEXUS_WAL_SEGMENT_SIZE", str(4 * 1024 * 1024)))

        config = EventLogConfig(
            wal_dir=Path(wal_dir),
            segment_size_bytes=segment_size,
            sync_mode=sync_mode,
        )
        event_log = create_event_log(config)
        if event_log:
            logger.info(
                "Event log WAL initialized (wal_dir=%s, sync_mode=%s)",
                wal_dir,
                sync_mode,
            )
        return event_log
    except Exception as e:
        logger.debug("Event log WAL not available: %s", e)
        return None


def _create_distributed_infra(
    dist: "DistributedConfig",
    metadata_store: "MetastoreABC",
    record_store: Any,
    coordination_url: str | None,
) -> tuple[Any, Any]:
    """Create event bus, event log, and lock manager.

    Returns (event_bus, lock_manager) tuple.
    Either event_bus or lock_manager may be None.

    Issue #2175: EventLog WAL is created and wired into EventBus
    for durable event persistence (WAL-first before pub/sub).
    """
    event_bus: Any = None
    lock_manager: Any = None

    try:
        # Initialize lock manager (uses Raft via metadata store)
        if dist.enable_locks:
            from nexus.lib.distributed_lock import LockStoreProtocol
            from nexus.raft.lock_manager import RaftLockManager

            if isinstance(metadata_store, LockStoreProtocol):
                lock_manager = RaftLockManager(metadata_store)
                logger.info("Distributed lock manager initialized (Raft consensus)")
            else:
                logger.warning(
                    "Distributed locks require LockStoreProtocol-compatible store, got %s. "
                    "Lock manager will not be initialized.",
                    type(metadata_store).__name__,
                )

        # Initialize event bus
        if dist.event_bus_backend == "nats":
            from nexus.system_services.event_subsystem.bus.factory import create_event_bus

            event_bus = create_event_bus(
                backend="nats",
                nats_url=dist.nats_url,
                record_store=record_store,
            )
            logger.info(
                "Distributed event bus initialized (NATS JetStream: %s, SSOT: PostgreSQL)",
                dist.nats_url,
            )
        elif dist.enable_events:
            from nexus.lib.env import get_dragonfly_url, get_redis_url

            coordination_url_resolved = coordination_url or get_redis_url()
            event_url_resolved = coordination_url_resolved or get_dragonfly_url()
            if event_url_resolved:
                from nexus.cache.dragonfly import DragonflyClient
                from nexus.system_services.event_subsystem.bus import RedisEventBus

                event_client = DragonflyClient(url=event_url_resolved)
                event_bus = RedisEventBus(
                    event_client,
                    record_store=record_store,
                )
                logger.info(
                    "Distributed event bus initialized (dragonfly: %s, SSOT: PostgreSQL)",
                    event_url_resolved,
                )

        # Issue #2175: Wire EventLog WAL into EventBus for durable persistence.
        # WAL-first pattern: events are durably appended before pub/sub broadcast.
        # Server lifespan may re-wire later (same idempotent pattern).
        if event_bus is not None and dist.enable_events:
            event_log = _create_event_log_wal()
            if event_log is not None and hasattr(event_bus, "set_event_log"):
                event_bus.set_event_log(event_log)
                logger.info("Event log WAL wired into EventBus (WAL-first durability)")

    except ImportError as e:
        logger.warning("Could not initialize distributed event system: %s", e)

    return event_bus, lock_manager


def _create_workflow_engine(
    record_store: Any, glob_match_fn: Any = None
) -> "WorkflowProtocol | None":
    """Create workflow engine with async store and DI.

    Args:
        record_store: RecordStoreABC instance (has async_session_factory property).
        glob_match_fn: Optional glob match function (Rust glob_fast in production).

    Returns workflow engine or None if unavailable.
    """
    if record_store is None:
        logger.warning("Workflows require record_store, skipping")
        return None
    try:
        from nexus.bricks.workflows.engine import WorkflowEngine
        from nexus.bricks.workflows.protocol import WorkflowServices
        from nexus.bricks.workflows.storage import WorkflowStore
        from nexus.contracts.constants import ROOT_ZONE_ID
        from nexus.storage.models import WorkflowExecutionModel, WorkflowModel

        workflow_store = WorkflowStore(
            record_store=record_store,
            workflow_model=WorkflowModel,
            execution_model=WorkflowExecutionModel,
            zone_id=ROOT_ZONE_ID,
        )
        services = WorkflowServices(glob_match=glob_match_fn)
        return WorkflowEngine(workflow_store=workflow_store, services=services)
    except Exception as e:
        logger.warning("Failed to create workflow engine: %s", e)
        return None
