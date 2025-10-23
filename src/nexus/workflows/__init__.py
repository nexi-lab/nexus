"""Workflow automation system for Nexus.

This module provides a lightweight workflow automation system that enables
AI agents to define and execute automated pipelines for document processing,
data transformation, and multi-step operations.
"""

from nexus.workflows.actions import BUILTIN_ACTIONS, BaseAction
from nexus.workflows.api import WorkflowAPI, get_workflow_api
from nexus.workflows.engine import WorkflowEngine, get_engine, init_engine
from nexus.workflows.loader import WorkflowLoader
from nexus.workflows.storage import WorkflowStore
from nexus.workflows.triggers import BUILTIN_TRIGGERS, BaseTrigger, TriggerManager
from nexus.workflows.types import (
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
    "get_workflow_api",
    # Core classes
    "WorkflowEngine",
    "WorkflowLoader",
    "WorkflowStore",
    "TriggerManager",
    # Engine functions
    "get_engine",
    "init_engine",
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
]
