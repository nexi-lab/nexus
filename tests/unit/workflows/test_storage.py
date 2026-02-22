"""Tests for workflow storage (async).

Consolidated API: load_workflow/delete_workflow/set_enabled/get_executions
each accept keyword-only ``workflow_id`` or ``name`` (exactly one required).
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nexus.bricks.workflows.storage import WorkflowStore
from nexus.bricks.workflows.types import (
    TriggerType,
    WorkflowAction,
    WorkflowDefinition,
    WorkflowExecution,
    WorkflowStatus,
    WorkflowTrigger,
)
from nexus.storage.models import Base, WorkflowExecutionModel, WorkflowModel


@pytest.fixture
async def async_engine():
    """Create in-memory async SQLite database."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def async_session_factory(async_engine):
    """Create async session factory."""
    return async_sessionmaker(bind=async_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def record_store(async_session_factory):
    """Wrap async_session_factory in a SimpleNamespace to mimic RecordStoreABC."""
    from types import SimpleNamespace

    return SimpleNamespace(async_session_factory=async_session_factory)


@pytest.fixture
def workflow_store(record_store):
    """Create workflow store with injected models."""
    return WorkflowStore(
        record_store,
        workflow_model=WorkflowModel,
        execution_model=WorkflowExecutionModel,
        zone_id="test-zone",
    )


class TestWorkflowStore:
    """Test WorkflowStore."""

    def test_create_store(self, workflow_store):
        """Test creating workflow store."""
        assert workflow_store is not None
        assert workflow_store.zone_id == "test-zone"

    # ------------------------------------------------------------------
    # save_workflow (unchanged)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_save_workflow(self, workflow_store):
        """Test saving a workflow."""
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            description="Test workflow",
            actions=[WorkflowAction(name="action1", type="python", config={"code": "pass"})],
        )

        workflow_id = await workflow_store.save_workflow(definition, enabled=True)
        assert workflow_id is not None
        assert isinstance(workflow_id, str)

    @pytest.mark.asyncio
    async def test_save_workflow_with_triggers(self, workflow_store):
        """Test saving workflow with triggers."""
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            triggers=[WorkflowTrigger(type=TriggerType.FILE_WRITE, config={"pattern": "*.md"})],
            actions=[WorkflowAction(name="action1", type="python")],
        )

        workflow_id = await workflow_store.save_workflow(definition)
        loaded = await workflow_store.load_workflow(workflow_id=workflow_id)

        assert loaded is not None
        assert len(loaded.triggers) == 1
        assert loaded.triggers[0].type == TriggerType.FILE_WRITE

    @pytest.mark.asyncio
    async def test_save_workflow_with_variables(self, workflow_store):
        """Test saving workflow with variables."""
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            variables={"env": "test", "debug": True},
            actions=[WorkflowAction(name="action1", type="python")],
        )

        workflow_id = await workflow_store.save_workflow(definition)
        loaded = await workflow_store.load_workflow(workflow_id=workflow_id)

        assert loaded is not None
        assert loaded.variables == {"env": "test", "debug": True}

    @pytest.mark.asyncio
    async def test_update_existing_workflow(self, workflow_store):
        """Test updating an existing workflow."""
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python")],
        )

        workflow_id1 = await workflow_store.save_workflow(definition)

        # Update the workflow
        updated_definition = WorkflowDefinition(
            name="test_workflow",
            version="2.0",
            description="Updated",
            actions=[WorkflowAction(name="action1", type="python")],
        )

        workflow_id2 = await workflow_store.save_workflow(updated_definition)

        # Should be the same workflow ID (update not create new)
        assert workflow_id1 == workflow_id2

        loaded = await workflow_store.load_workflow(workflow_id=workflow_id1)
        assert loaded.version == "2.0"
        assert loaded.description == "Updated"

    # ------------------------------------------------------------------
    # load_workflow (consolidated: workflow_id= or name=)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_load_workflow_by_id(self, workflow_store):
        """Test loading a workflow by ID."""
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            description="Test workflow",
            actions=[WorkflowAction(name="action1", type="python")],
        )

        workflow_id = await workflow_store.save_workflow(definition)
        loaded = await workflow_store.load_workflow(workflow_id=workflow_id)

        assert loaded is not None
        assert loaded.name == "test_workflow"
        assert loaded.version == "1.0"
        assert loaded.description == "Test workflow"
        assert len(loaded.actions) == 1

    @pytest.mark.asyncio
    async def test_load_workflow_by_id_nonexistent(self, workflow_store):
        """Test loading non-existent workflow by ID."""
        loaded = await workflow_store.load_workflow(workflow_id=str(uuid.uuid4()))
        assert loaded is None

    @pytest.mark.asyncio
    async def test_load_workflow_by_name(self, workflow_store):
        """Test loading workflow by name (keyword arg)."""
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python")],
        )

        await workflow_store.save_workflow(definition)
        loaded = await workflow_store.load_workflow(name="test_workflow")

        assert loaded is not None
        assert loaded.name == "test_workflow"

    @pytest.mark.asyncio
    async def test_load_workflow_by_name_nonexistent(self, workflow_store):
        """Test loading non-existent workflow by name."""
        loaded = await workflow_store.load_workflow(name="nonexistent")
        assert loaded is None

    @pytest.mark.asyncio
    async def test_load_workflow_no_args_raises(self, workflow_store):
        """Test that calling load_workflow with no args raises ValueError."""
        with pytest.raises(ValueError, match="workflow_id or name"):
            await workflow_store.load_workflow()

    # ------------------------------------------------------------------
    # list_workflows (unchanged)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_list_workflows(self, workflow_store):
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

        await workflow_store.save_workflow(definition1, enabled=True)
        await workflow_store.save_workflow(definition2, enabled=False)

        workflows = await workflow_store.list_workflows()
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

    @pytest.mark.asyncio
    async def test_list_workflows_empty(self, workflow_store):
        """Test listing workflows when none exist."""
        workflows = await workflow_store.list_workflows()
        assert workflows == []

    # ------------------------------------------------------------------
    # delete_workflow (consolidated: workflow_id= or name=)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_delete_workflow_by_id(self, workflow_store):
        """Test deleting a workflow by ID."""
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python")],
        )

        workflow_id = await workflow_store.save_workflow(definition)
        result = await workflow_store.delete_workflow(workflow_id=workflow_id)
        assert result is True

        # Verify it's deleted
        loaded = await workflow_store.load_workflow(workflow_id=workflow_id)
        assert loaded is None

    @pytest.mark.asyncio
    async def test_delete_workflow_by_id_nonexistent(self, workflow_store):
        """Test deleting non-existent workflow by ID."""
        result = await workflow_store.delete_workflow(workflow_id=str(uuid.uuid4()))
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_workflow_by_name(self, workflow_store):
        """Test deleting workflow by name."""
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python")],
        )

        await workflow_store.save_workflow(definition)
        result = await workflow_store.delete_workflow(name="test_workflow")
        assert result is True

        # Verify it's deleted
        loaded = await workflow_store.load_workflow(name="test_workflow")
        assert loaded is None

    @pytest.mark.asyncio
    async def test_delete_workflow_by_name_nonexistent(self, workflow_store):
        """Test deleting non-existent workflow by name."""
        result = await workflow_store.delete_workflow(name="nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_workflow_no_args_raises(self, workflow_store):
        """Test that calling delete_workflow with no args raises ValueError."""
        with pytest.raises(ValueError, match="workflow_id or name"):
            await workflow_store.delete_workflow()

    # ------------------------------------------------------------------
    # set_enabled (consolidated: enabled, *, workflow_id= or name=)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_set_enabled_by_id(self, workflow_store):
        """Test enabling/disabling workflow by ID."""
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python")],
        )

        workflow_id = await workflow_store.save_workflow(definition, enabled=True)

        # Disable
        result = await workflow_store.set_enabled(False, workflow_id=workflow_id)
        assert result is True

        workflows = await workflow_store.list_workflows()
        assert workflows[0]["enabled"] is False

        # Enable
        result = await workflow_store.set_enabled(True, workflow_id=workflow_id)
        assert result is True

        workflows = await workflow_store.list_workflows()
        assert workflows[0]["enabled"] is True

    @pytest.mark.asyncio
    async def test_set_enabled_by_id_nonexistent(self, workflow_store):
        """Test setting enabled on non-existent workflow by ID."""
        result = await workflow_store.set_enabled(True, workflow_id=str(uuid.uuid4()))
        assert result is False

    @pytest.mark.asyncio
    async def test_set_enabled_by_name(self, workflow_store):
        """Test enabling/disabling workflow by name."""
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python")],
        )

        await workflow_store.save_workflow(definition, enabled=True)

        # Disable
        result = await workflow_store.set_enabled(False, name="test_workflow")
        assert result is True

        workflows = await workflow_store.list_workflows()
        assert workflows[0]["enabled"] is False

    @pytest.mark.asyncio
    async def test_set_enabled_by_name_nonexistent(self, workflow_store):
        """Test setting enabled by name on non-existent workflow."""
        result = await workflow_store.set_enabled(True, name="nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_set_enabled_no_args_raises(self, workflow_store):
        """Test that calling set_enabled with no identifier raises ValueError."""
        with pytest.raises(ValueError, match="workflow_id or name"):
            await workflow_store.set_enabled(True)

    # ------------------------------------------------------------------
    # save_execution (unchanged)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_save_execution(self, workflow_store):
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

        execution_id = await workflow_store.save_execution(execution)
        assert execution_id is not None
        assert isinstance(execution_id, str)

    # ------------------------------------------------------------------
    # get_executions (consolidated: workflow_id= or name=, single session)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_executions_by_id(self, workflow_store):
        """Test getting execution history by workflow ID."""
        from datetime import UTC, datetime

        # Save a workflow first
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python")],
        )
        workflow_id_str = await workflow_store.save_workflow(definition)

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
            await workflow_store.save_execution(execution)

        # Get executions
        executions = await workflow_store.get_executions(workflow_id=workflow_id_str, limit=10)
        assert len(executions) == 3

    @pytest.mark.asyncio
    async def test_get_executions_with_limit(self, workflow_store):
        """Test getting execution history with limit."""
        from datetime import UTC, datetime

        # Save a workflow first
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python")],
        )
        workflow_id_str = await workflow_store.save_workflow(definition)

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
            await workflow_store.save_execution(execution)

        # Get executions with limit
        executions = await workflow_store.get_executions(workflow_id=workflow_id_str, limit=3)
        assert len(executions) == 3

    @pytest.mark.asyncio
    async def test_get_executions_by_name(self, workflow_store):
        """Test getting execution history by workflow name (single session JOIN)."""
        from datetime import UTC, datetime

        # Save a workflow first
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python")],
        )
        workflow_id_str = await workflow_store.save_workflow(definition)

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
        await workflow_store.save_execution(execution)

        # Get executions by name
        executions = await workflow_store.get_executions(name="test_workflow")
        assert len(executions) == 1

    @pytest.mark.asyncio
    async def test_get_executions_by_name_nonexistent(self, workflow_store):
        """Test getting execution history for non-existent workflow by name."""
        executions = await workflow_store.get_executions(name="nonexistent")
        assert executions == []

    @pytest.mark.asyncio
    async def test_get_executions_no_args_raises(self, workflow_store):
        """Test that calling get_executions with no args raises ValueError."""
        with pytest.raises(ValueError, match="workflow_id or name"):
            await workflow_store.get_executions()

    # ------------------------------------------------------------------
    # Utility tests (unchanged)
    # ------------------------------------------------------------------

    def test_compute_hash(self, workflow_store):
        """Test computing workflow hash."""
        yaml_content1 = "name: test\nversion: 1.0"
        yaml_content2 = "name: test\nversion: 2.0"

        hash1 = workflow_store._compute_hash(yaml_content1)
        hash2 = workflow_store._compute_hash(yaml_content2)

        assert hash1 != hash2
        assert len(hash1) == 64  # SHA256 hex
        assert len(hash2) == 64

    def test_get_zone_id(self, workflow_store):
        """Test getting zone ID."""
        assert workflow_store._get_zone_id() == "test-zone"

    def test_default_zone_id(self, async_session_factory):
        """Test default zone ID (uses ROOT_ZONE_ID = 'root')."""
        from types import SimpleNamespace

        store = WorkflowStore(
            SimpleNamespace(async_session_factory=async_session_factory),
            workflow_model=WorkflowModel,
            execution_model=WorkflowExecutionModel,
        )
        assert store._get_zone_id() == "root"
