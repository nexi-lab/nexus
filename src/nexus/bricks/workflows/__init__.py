"""Workflow automation system for Nexus.

This module provides a lightweight workflow automation system that enables
AI agents to define and execute automated pipelines for document processing,
data transformation, and multi-step operations.
"""

from nexus.bricks.workflows.actions import BUILTIN_ACTIONS, BaseAction
from nexus.bricks.workflows.api import WorkflowAPI
from nexus.bricks.workflows.engine import WorkflowEngine
from nexus.bricks.workflows.loader import WorkflowLoader
from nexus.bricks.workflows.protocol import (
    GlobMatchFn,
    MetadataStoreProtocol,
    NexusOperationsProtocol,
    WorkflowProtocol,
    WorkflowServices,
)
from nexus.bricks.workflows.storage import WorkflowStore
from nexus.bricks.workflows.triggers import (
    BUILTIN_TRIGGERS,
    BaseTrigger,
    TriggerFactory,
    TriggerManager,
)
from nexus.bricks.workflows.types import (
    ActionResult,
    TriggerType,
    WorkflowAction,
    WorkflowContext,
    WorkflowDefinition,
    WorkflowExecution,
    WorkflowStatus,
    WorkflowTrigger,
)

__all__ = [
    # High-level API
    "WorkflowAPI",
    # Core classes
    "WorkflowEngine",
    "WorkflowLoader",
    "WorkflowStore",
    "TriggerManager",
    # Protocols
    "WorkflowProtocol",
    "WorkflowServices",
    "GlobMatchFn",
    "NexusOperationsProtocol",
    "MetadataStoreProtocol",
    # Types
    "WorkflowDefinition",
    "WorkflowAction",
    "WorkflowTrigger",
    "WorkflowContext",
    "WorkflowExecution",
    "WorkflowStatus",
    "TriggerType",
    "ActionResult",
    # Base classes for extensions
    "BaseAction",
    "BaseTrigger",
    # Built-in registries
    "BUILTIN_ACTIONS",
    "BUILTIN_TRIGGERS",
    "TriggerFactory",
]
