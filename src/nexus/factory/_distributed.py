"""Distributed infrastructure — event bus, lock manager, workflow engine."""

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.bricks.workflows.protocol import WorkflowProtocol
    from nexus.core.config import DistributedConfig
    from nexus.core.metastore import MetastoreABC

logger = logging.getLogger(__name__)


def _create_distributed_infra(
    dist: "DistributedConfig",
    metadata_store: "MetastoreABC",
    record_store: Any,
    coordination_url: str | None,
    *,
    zone_id: str = "root",
) -> tuple[Any, Any]:
    """Create event bus and lock manager.

    Returns (event_bus, lock_manager) tuple.
    Either event_bus or lock_manager may be None.
    """
    event_bus: Any = None
    lock_manager: Any = None

    try:
        # Initialize lock manager (uses Raft via metadata store)
        if dist.enable_locks:
            from nexus.lib.distributed_lock import LockStoreProtocol
            from nexus.raft.lock_manager import RaftLockManager

            if isinstance(metadata_store, LockStoreProtocol):
                lock_manager = RaftLockManager(metadata_store, zone_id=zone_id)
                logger.info("Distributed lock manager initialized (Raft, zone=%s)", zone_id)
            else:
                logger.warning(
                    "Distributed locks require LockStoreProtocol-compatible store, got %s. "
                    "Lock manager will not be initialized.",
                    type(metadata_store).__name__,
                )

        # Initialize event bus
        if dist.event_bus_backend == "nats" and dist.enable_events:
            from nexus.system_services.event_bus.factory import create_event_bus

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
                from nexus.system_services.event_bus import RedisEventBus

                event_client = DragonflyClient(url=event_url_resolved)
                event_bus = RedisEventBus(
                    event_client,
                    record_store=record_store,
                )
                logger.info(
                    "Distributed event bus initialized (dragonfly: %s, SSOT: PostgreSQL)",
                    event_url_resolved,
                )

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
