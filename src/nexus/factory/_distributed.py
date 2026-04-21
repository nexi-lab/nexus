"""Distributed infrastructure — workflow engine.

Event bus creation is inlined in _system.py._boot_services().
Advisory locks are kernel-owned (Rust LockManager with optional Raft backend).
"""

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.bricks.workflows.protocol import WorkflowProtocol

logger = logging.getLogger(__name__)


def _create_workflow_engine(
    record_store: Any,
    glob_match_fn: Any = None,
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
