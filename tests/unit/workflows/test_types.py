"""Tests for workflow types and data structures."""

import uuid
from datetime import UTC, datetime

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


class TestWorkflowStatus:
    """Test WorkflowStatus enum."""

    def test_all_statuses(self):
        """Test all workflow statuses are defined."""
        assert WorkflowStatus.PENDING == "pending"
        assert WorkflowStatus.RUNNING == "running"
        assert WorkflowStatus.SUCCEEDED == "succeeded"
        assert WorkflowStatus.FAILED == "failed"
        assert WorkflowStatus.CANCELLED == "cancelled"


class TestTriggerType:
    """Test TriggerType enum."""

    def test_all_trigger_types(self):
        """Test all trigger types are defined."""
        assert TriggerType.FILE_WRITE == "file_write"
        assert TriggerType.FILE_DELETE == "file_delete"
        assert TriggerType.FILE_RENAME == "file_rename"
        assert TriggerType.METADATA_CHANGE == "metadata_change"
        assert TriggerType.SCHEDULE == "schedule"
        assert TriggerType.WEBHOOK == "webhook"
        assert TriggerType.MANUAL == "manual"


class TestWorkflowAction:
    """Test WorkflowAction dataclass."""

    def test_create_basic_action(self):
        """Test creating a basic action."""
        action = WorkflowAction(name="test_action", type="parse")
        assert action.name == "test_action"
        assert action.type == "parse"
        assert action.config == {}

    def test_create_action_with_config(self):
        """Test creating action with config."""
        config = {"parser": "markdown", "extract": "metadata"}
        action = WorkflowAction(name="parse_doc", type="parse", config=config)
        assert action.name == "parse_doc"
        assert action.type == "parse"
        assert action.config == config


class TestWorkflowTrigger:
    """Test WorkflowTrigger dataclass."""

    def test_create_basic_trigger(self):
        """Test creating a basic trigger."""
        trigger = WorkflowTrigger(type=TriggerType.FILE_WRITE)
        assert trigger.type == TriggerType.FILE_WRITE
        assert trigger.config == {}

    def test_create_trigger_with_config(self):
        """Test creating trigger with config."""
        config = {"pattern": "*.md", "recursive": True}
        trigger = WorkflowTrigger(type=TriggerType.FILE_WRITE, config=config)
        assert trigger.type == TriggerType.FILE_WRITE
        assert trigger.config == config


class TestWorkflowDefinition:
    """Test WorkflowDefinition dataclass."""

    def test_create_minimal_definition(self):
        """Test creating minimal workflow definition."""
        definition = WorkflowDefinition(name="test_workflow", version="1.0")
        assert definition.name == "test_workflow"
        assert definition.version == "1.0"
        assert definition.description == ""
        assert definition.triggers == []
        assert definition.actions == []
        assert definition.variables == {}

    def test_create_complete_definition(self):
        """Test creating complete workflow definition."""
        triggers = [WorkflowTrigger(type=TriggerType.FILE_WRITE, config={"pattern": "*.txt"})]
        actions = [WorkflowAction(name="parse", type="parse")]
        variables = {"env": "test"}

        definition = WorkflowDefinition(
            name="complete_workflow",
            version="2.0",
            description="A complete workflow",
            triggers=triggers,
            actions=actions,
            variables=variables,
        )

        assert definition.name == "complete_workflow"
        assert definition.version == "2.0"
        assert definition.description == "A complete workflow"
        assert len(definition.triggers) == 1
        assert len(definition.actions) == 1
        assert definition.variables == variables


class TestWorkflowContext:
    """Test WorkflowContext dataclass."""

    def test_create_basic_context(self):
        """Test creating basic workflow context."""
        workflow_id = uuid.uuid4()
        execution_id = uuid.uuid4()
        tenant_id = uuid.uuid4()

        context = WorkflowContext(
            workflow_id=workflow_id,
            execution_id=execution_id,
            tenant_id=tenant_id,
            trigger_type=TriggerType.MANUAL,
        )

        assert context.workflow_id == workflow_id
        assert context.execution_id == execution_id
        assert context.tenant_id == tenant_id
        assert context.trigger_type == TriggerType.MANUAL
        assert context.trigger_context == {}
        assert context.variables == {}
        assert context.file_path is None
        assert context.file_metadata is None

    def test_create_context_with_file(self):
        """Test creating context with file information."""
        workflow_id = uuid.uuid4()
        execution_id = uuid.uuid4()
        tenant_id = uuid.uuid4()

        context = WorkflowContext(
            workflow_id=workflow_id,
            execution_id=execution_id,
            tenant_id=tenant_id,
            trigger_type=TriggerType.FILE_WRITE,
            file_path="/test/file.txt",
            file_metadata={"size": 1024, "type": "text"},
            variables={"env": "prod"},
        )

        assert context.file_path == "/test/file.txt"
        assert context.file_metadata == {"size": 1024, "type": "text"}
        assert context.variables == {"env": "prod"}


class TestActionResult:
    """Test ActionResult dataclass."""

    def test_create_successful_result(self):
        """Test creating successful action result."""
        result = ActionResult(
            action_name="test_action",
            success=True,
            output={"data": "processed"},
            duration_ms=123.45,
        )

        assert result.action_name == "test_action"
        assert result.success is True
        assert result.output == {"data": "processed"}
        assert result.error is None
        assert result.duration_ms == 123.45

    def test_create_failed_result(self):
        """Test creating failed action result."""
        result = ActionResult(
            action_name="test_action", success=False, error="Something went wrong"
        )

        assert result.action_name == "test_action"
        assert result.success is False
        assert result.output is None
        assert result.error == "Something went wrong"


class TestWorkflowExecution:
    """Test WorkflowExecution dataclass."""

    def test_create_basic_execution(self):
        """Test creating basic workflow execution."""
        execution_id = uuid.uuid4()
        workflow_id = uuid.uuid4()

        execution = WorkflowExecution(
            execution_id=execution_id,
            workflow_id=workflow_id,
            workflow_name="test_workflow",
            status=WorkflowStatus.PENDING,
            trigger_type=TriggerType.MANUAL,
            trigger_context={},
        )

        assert execution.execution_id == execution_id
        assert execution.workflow_id == workflow_id
        assert execution.workflow_name == "test_workflow"
        assert execution.status == WorkflowStatus.PENDING
        assert execution.trigger_type == TriggerType.MANUAL
        assert execution.started_at is None
        assert execution.completed_at is None
        assert execution.actions_completed == 0
        assert execution.actions_total == 0
        assert execution.action_results == []
        assert execution.error_message is None

    def test_create_complete_execution(self):
        """Test creating complete workflow execution."""
        execution_id = uuid.uuid4()
        workflow_id = uuid.uuid4()
        started_at = datetime.now(UTC)
        completed_at = datetime.now(UTC)

        action_results = [
            ActionResult(action_name="action1", success=True),
            ActionResult(action_name="action2", success=True),
        ]

        execution = WorkflowExecution(
            execution_id=execution_id,
            workflow_id=workflow_id,
            workflow_name="test_workflow",
            status=WorkflowStatus.SUCCEEDED,
            trigger_type=TriggerType.FILE_WRITE,
            trigger_context={"file_path": "/test.txt"},
            started_at=started_at,
            completed_at=completed_at,
            actions_completed=2,
            actions_total=2,
            action_results=action_results,
        )

        assert execution.status == WorkflowStatus.SUCCEEDED
        assert execution.started_at == started_at
        assert execution.completed_at == completed_at
        assert execution.actions_completed == 2
        assert execution.actions_total == 2
        assert len(execution.action_results) == 2
