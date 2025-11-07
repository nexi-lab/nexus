"""Core data structures for workflow system."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class WorkflowStatus(StrEnum):
    """Status of a workflow execution."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TriggerType(StrEnum):
    """Types of workflow triggers."""

    FILE_WRITE = "file_write"
    FILE_DELETE = "file_delete"
    FILE_RENAME = "file_rename"
    METADATA_CHANGE = "metadata_change"
    SCHEDULE = "schedule"
    WEBHOOK = "webhook"
    MANUAL = "manual"


@dataclass
class WorkflowAction:
    """Definition of a single workflow action."""

    name: str
    type: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowTrigger:
    """Definition of a workflow trigger."""

    type: TriggerType
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowDefinition:
    """Complete workflow definition."""

    name: str
    version: str
    description: str = ""
    triggers: list[WorkflowTrigger] = field(default_factory=list)
    actions: list[WorkflowAction] = field(default_factory=list)
    variables: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowContext:
    """Runtime context for workflow execution."""

    workflow_id: UUID
    execution_id: UUID
    tenant_id: UUID | None  # Allow None for default/global workflows
    trigger_type: TriggerType
    trigger_context: dict[str, Any] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)
    file_path: str | None = None
    file_metadata: dict[str, Any] | None = None


@dataclass
class ActionResult:
    """Result of an action execution."""

    action_name: str
    success: bool
    output: Any | None = None
    error: str | None = None
    duration_ms: float = 0.0


@dataclass
class WorkflowExecution:
    """Record of a workflow execution."""

    execution_id: UUID
    workflow_id: UUID
    workflow_name: str
    status: WorkflowStatus
    trigger_type: TriggerType
    trigger_context: dict[str, Any]
    started_at: datetime | None = None
    completed_at: datetime | None = None
    actions_completed: int = 0
    actions_total: int = 0
    action_results: list[ActionResult] = field(default_factory=list)
    error_message: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
