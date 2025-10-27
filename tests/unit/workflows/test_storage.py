"""Tests for workflow storage."""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.storage.models import Base
from nexus.workflows.storage import WorkflowStore
from nexus.workflows.types import (
    TriggerType,
    WorkflowAction,
    WorkflowDefinition,
    WorkflowExecution,
    WorkflowStatus,
    WorkflowTrigger,
)


@pytest.fixture
def engine():
    """Create in-memory SQLite database."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session_factory(engine):
    """Create session factory."""
    return sessionmaker(bind=engine)


@pytest.fixture
def workflow_store(session_factory):
    """Create workflow store."""
    return WorkflowStore(session_factory, tenant_id="test-tenant")


class TestWorkflowStore:
    """Test WorkflowStore."""

    def test_create_store(self, workflow_store):
        """Test creating workflow store."""
        assert workflow_store is not None
        assert workflow_store.tenant_id == "test-tenant"

    def test_save_workflow(self, workflow_store):
        """Test saving a workflow."""
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            description="Test workflow",
            actions=[WorkflowAction(name="action1", type="python", config={"code": "pass"})],
        )

        workflow_id = workflow_store.save_workflow(definition, enabled=True)
        assert workflow_id is not None
        assert isinstance(workflow_id, str)

    def test_save_workflow_with_triggers(self, workflow_store):
        """Test saving workflow with triggers."""
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            triggers=[WorkflowTrigger(type=TriggerType.FILE_WRITE, config={"pattern": "*.md"})],
            actions=[WorkflowAction(name="action1", type="python")],
        )

        workflow_id = workflow_store.save_workflow(definition)
        loaded = workflow_store.load_workflow(workflow_id)

        assert loaded is not None
        assert len(loaded.triggers) == 1
        assert loaded.triggers[0].type == TriggerType.FILE_WRITE

    def test_save_workflow_with_variables(self, workflow_store):
        """Test saving workflow with variables."""
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            variables={"env": "test", "debug": True},
            actions=[WorkflowAction(name="action1", type="python")],
        )

        workflow_id = workflow_store.save_workflow(definition)
        loaded = workflow_store.load_workflow(workflow_id)

        assert loaded is not None
        assert loaded.variables == {"env": "test", "debug": True}

    def test_update_existing_workflow(self, workflow_store):
        """Test updating an existing workflow."""
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python")],
        )

        workflow_id1 = workflow_store.save_workflow(definition)

        # Update the workflow
        updated_definition = WorkflowDefinition(
            name="test_workflow",
            version="2.0",
            description="Updated",
            actions=[WorkflowAction(name="action1", type="python")],
        )

        workflow_id2 = workflow_store.save_workflow(updated_definition)

        # Should be the same workflow ID (update not create new)
        assert workflow_id1 == workflow_id2

        loaded = workflow_store.load_workflow(workflow_id1)
        assert loaded.version == "2.0"
        assert loaded.description == "Updated"

    def test_load_workflow(self, workflow_store):
        """Test loading a workflow."""
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            description="Test workflow",
            actions=[WorkflowAction(name="action1", type="python")],
        )

        workflow_id = workflow_store.save_workflow(definition)
        loaded = workflow_store.load_workflow(workflow_id)

        assert loaded is not None
        assert loaded.name == "test_workflow"
        assert loaded.version == "1.0"
        assert loaded.description == "Test workflow"
        assert len(loaded.actions) == 1

    def test_load_nonexistent_workflow(self, workflow_store):
        """Test loading non-existent workflow."""
        loaded = workflow_store.load_workflow(str(uuid.uuid4()))
        assert loaded is None

    def test_load_workflow_by_name(self, workflow_store):
        """Test loading workflow by name."""
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python")],
        )

        workflow_store.save_workflow(definition)
        loaded = workflow_store.load_workflow_by_name("test_workflow")

        assert loaded is not None
        assert loaded.name == "test_workflow"

    def test_load_workflow_by_name_nonexistent(self, workflow_store):
        """Test loading non-existent workflow by name."""
        loaded = workflow_store.load_workflow_by_name("nonexistent")
        assert loaded is None

    def test_list_workflows(self, workflow_store):
        """Test listing workflows."""
        definition1 = WorkflowDefinition(
            name="workflow1",
            version="1.0",
            description="First workflow",
            triggers=[WorkflowTrigger(type=TriggerType.FILE_WRITE, config={"pattern": "*.md"})],
            actions=[WorkflowAction(name="action1", type="python")],
        )
        definition2 = WorkflowDefinition(
            name="workflow2",
            version="2.0",
            description="Second workflow",
            actions=[
                WorkflowAction(name="action1", type="python"),
                WorkflowAction(name="action2", type="bash"),
            ],
        )

        workflow_store.save_workflow(definition1, enabled=True)
        workflow_store.save_workflow(definition2, enabled=False)

        workflows = workflow_store.list_workflows()
        assert len(workflows) == 2

        # Check first workflow
        wf1 = next(w for w in workflows if w["name"] == "workflow1")
        assert wf1["version"] == "1.0"
        assert wf1["description"] == "First workflow"
        assert wf1["enabled"] is True
        assert wf1["triggers"] == 1
        assert wf1["actions"] == 1

        # Check second workflow
        wf2 = next(w for w in workflows if w["name"] == "workflow2")
        assert wf2["version"] == "2.0"
        assert wf2["enabled"] is False
        assert wf2["triggers"] == 0
        assert wf2["actions"] == 2

    def test_list_workflows_empty(self, workflow_store):
        """Test listing workflows when none exist."""
        workflows = workflow_store.list_workflows()
        assert workflows == []

    def test_delete_workflow(self, workflow_store):
        """Test deleting a workflow."""
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python")],
        )

        workflow_id = workflow_store.save_workflow(definition)
        result = workflow_store.delete_workflow(workflow_id)
        assert result is True

        # Verify it's deleted
        loaded = workflow_store.load_workflow(workflow_id)
        assert loaded is None

    def test_delete_nonexistent_workflow(self, workflow_store):
        """Test deleting non-existent workflow."""
        result = workflow_store.delete_workflow(str(uuid.uuid4()))
        assert result is False

    def test_delete_workflow_by_name(self, workflow_store):
        """Test deleting workflow by name."""
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python")],
        )

        workflow_store.save_workflow(definition)
        result = workflow_store.delete_workflow_by_name("test_workflow")
        assert result is True

        # Verify it's deleted
        loaded = workflow_store.load_workflow_by_name("test_workflow")
        assert loaded is None

    def test_delete_workflow_by_name_nonexistent(self, workflow_store):
        """Test deleting non-existent workflow by name."""
        result = workflow_store.delete_workflow_by_name("nonexistent")
        assert result is False

    def test_set_enabled(self, workflow_store):
        """Test enabling/disabling workflow."""
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python")],
        )

        workflow_id = workflow_store.save_workflow(definition, enabled=True)

        # Disable
        result = workflow_store.set_enabled(workflow_id, False)
        assert result is True

        workflows = workflow_store.list_workflows()
        assert workflows[0]["enabled"] is False

        # Enable
        result = workflow_store.set_enabled(workflow_id, True)
        assert result is True

        workflows = workflow_store.list_workflows()
        assert workflows[0]["enabled"] is True

    def test_set_enabled_nonexistent(self, workflow_store):
        """Test setting enabled on non-existent workflow."""
        result = workflow_store.set_enabled(str(uuid.uuid4()), True)
        assert result is False

    def test_set_enabled_by_name(self, workflow_store):
        """Test enabling/disabling workflow by name."""
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python")],
        )

        workflow_store.save_workflow(definition, enabled=True)

        # Disable
        result = workflow_store.set_enabled_by_name("test_workflow", False)
        assert result is True

        workflows = workflow_store.list_workflows()
        assert workflows[0]["enabled"] is False

    def test_set_enabled_by_name_nonexistent(self, workflow_store):
        """Test setting enabled by name on non-existent workflow."""
        result = workflow_store.set_enabled_by_name("nonexistent", True)
        assert result is False

    def test_save_execution(self, workflow_store):
        """Test saving workflow execution."""
        from datetime import UTC, datetime

        execution = WorkflowExecution(
            execution_id=uuid.uuid4(),
            workflow_id=uuid.uuid4(),
            workflow_name="test_workflow",
            status=WorkflowStatus.SUCCEEDED,
            trigger_type=TriggerType.MANUAL,
            trigger_context={},
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            actions_completed=2,
            actions_total=2,
        )

        execution_id = workflow_store.save_execution(execution)
        assert execution_id is not None
        assert isinstance(execution_id, str)

    def test_get_executions(self, workflow_store):
        """Test getting execution history."""
        from datetime import UTC, datetime

        # Save a workflow first
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python")],
        )
        workflow_id_str = workflow_store.save_workflow(definition)

        # Create executions
        for _i in range(3):
            execution = WorkflowExecution(
                execution_id=uuid.uuid4(),
                workflow_id=uuid.UUID(workflow_id_str),
                workflow_name="test_workflow",
                status=WorkflowStatus.SUCCEEDED,
                trigger_type=TriggerType.MANUAL,
                trigger_context={},
                started_at=datetime.now(UTC),
                actions_total=1,
            )
            workflow_store.save_execution(execution)

        # Get executions
        executions = workflow_store.get_executions(workflow_id_str, limit=10)
        assert len(executions) == 3

    def test_get_executions_with_limit(self, workflow_store):
        """Test getting execution history with limit."""
        from datetime import UTC, datetime

        # Save a workflow first
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python")],
        )
        workflow_id_str = workflow_store.save_workflow(definition)

        # Create more executions than limit
        for _i in range(5):
            execution = WorkflowExecution(
                execution_id=uuid.uuid4(),
                workflow_id=uuid.UUID(workflow_id_str),
                workflow_name="test_workflow",
                status=WorkflowStatus.SUCCEEDED,
                trigger_type=TriggerType.MANUAL,
                trigger_context={},
                started_at=datetime.now(UTC),
                actions_total=1,
            )
            workflow_store.save_execution(execution)

        # Get executions with limit
        executions = workflow_store.get_executions(workflow_id_str, limit=3)
        assert len(executions) == 3

    def test_get_executions_by_name(self, workflow_store):
        """Test getting execution history by workflow name."""
        from datetime import UTC, datetime

        # Save a workflow first
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python")],
        )
        workflow_id_str = workflow_store.save_workflow(definition)

        # Create execution
        execution = WorkflowExecution(
            execution_id=uuid.uuid4(),
            workflow_id=uuid.UUID(workflow_id_str),
            workflow_name="test_workflow",
            status=WorkflowStatus.SUCCEEDED,
            trigger_type=TriggerType.MANUAL,
            trigger_context={},
            started_at=datetime.now(UTC),
            actions_total=1,
        )
        workflow_store.save_execution(execution)

        # Get executions by name
        executions = workflow_store.get_executions_by_name("test_workflow")
        assert len(executions) == 1

    def test_get_executions_by_name_nonexistent(self, workflow_store):
        """Test getting execution history for non-existent workflow."""
        executions = workflow_store.get_executions_by_name("nonexistent")
        assert executions == []

    def test_compute_hash(self, workflow_store):
        """Test computing workflow hash."""
        yaml_content1 = "name: test\nversion: 1.0"
        yaml_content2 = "name: test\nversion: 2.0"

        hash1 = workflow_store._compute_hash(yaml_content1)
        hash2 = workflow_store._compute_hash(yaml_content2)

        assert hash1 != hash2
        assert len(hash1) == 64  # SHA256 hex
        assert len(hash2) == 64

    def test_get_tenant_id(self, workflow_store):
        """Test getting tenant ID."""
        assert workflow_store._get_tenant_id() == "test-tenant"

    def test_default_tenant_id(self, session_factory):
        """Test default tenant ID."""
        store = WorkflowStore(session_factory)
        assert store._get_tenant_id() == "default"
