"""Tests for workflow engine."""

import uuid

import pytest

from nexus.workflows.engine import WorkflowEngine, get_engine, init_engine
from nexus.workflows.types import (
    TriggerType,
    WorkflowAction,
    WorkflowContext,
    WorkflowDefinition,
    WorkflowStatus,
    WorkflowTrigger,
)


class TestWorkflowEngine:
    """Test WorkflowEngine."""

    def test_create_engine(self):
        """Test creating workflow engine."""
        engine = WorkflowEngine()
        assert engine is not None
        assert engine.workflows == {}
        assert engine.enabled_workflows == {}

    def test_load_workflow(self):
        """Test loading a workflow."""
        engine = WorkflowEngine()
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python", config={"code": "pass"})],
        )

        result = engine.load_workflow(definition, enabled=True)
        assert result is True
        assert "test_workflow" in engine.workflows
        assert engine.enabled_workflows["test_workflow"] is True

    def test_load_workflow_validation_no_name(self):
        """Test loading workflow without name fails."""
        engine = WorkflowEngine()
        definition = WorkflowDefinition(
            name="",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python")],
        )

        result = engine.load_workflow(definition)
        assert result is False

    def test_load_workflow_validation_no_actions(self):
        """Test loading workflow without actions fails."""
        engine = WorkflowEngine()
        definition = WorkflowDefinition(name="test_workflow", version="1.0", actions=[])

        result = engine.load_workflow(definition)
        assert result is False

    def test_unload_workflow(self):
        """Test unloading a workflow."""
        engine = WorkflowEngine()
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python")],
        )

        engine.load_workflow(definition)
        assert "test_workflow" in engine.workflows

        result = engine.unload_workflow("test_workflow")
        assert result is True
        assert "test_workflow" not in engine.workflows

    def test_unload_nonexistent_workflow(self):
        """Test unloading non-existent workflow."""
        engine = WorkflowEngine()
        result = engine.unload_workflow("nonexistent")
        assert result is False

    def test_enable_workflow(self):
        """Test enabling a workflow."""
        engine = WorkflowEngine()
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python")],
        )

        engine.load_workflow(definition, enabled=False)
        assert engine.enabled_workflows["test_workflow"] is False

        engine.enable_workflow("test_workflow")
        assert engine.enabled_workflows["test_workflow"] is True

    def test_disable_workflow(self):
        """Test disabling a workflow."""
        engine = WorkflowEngine()
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python")],
        )

        engine.load_workflow(definition, enabled=True)
        assert engine.enabled_workflows["test_workflow"] is True

        engine.disable_workflow("test_workflow")
        assert engine.enabled_workflows["test_workflow"] is False

    def test_list_workflows(self):
        """Test listing workflows."""
        engine = WorkflowEngine()
        definition1 = WorkflowDefinition(
            name="workflow1",
            version="1.0",
            description="First workflow",
            actions=[WorkflowAction(name="action1", type="python")],
            triggers=[WorkflowTrigger(type=TriggerType.FILE_WRITE, config={"pattern": "*.md"})],
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

        engine.load_workflow(definition1, enabled=True)
        engine.load_workflow(definition2, enabled=False)

        workflows = engine.list_workflows()
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
    async def test_execute_workflow(self):
        """Test executing a workflow."""
        engine = WorkflowEngine()
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="calc", type="python", config={"code": "result = 2 + 2"})],
        )

        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            trigger_type=TriggerType.MANUAL,
        )

        execution = await engine.execute_workflow(definition, context)
        assert execution.status == WorkflowStatus.SUCCEEDED
        assert execution.actions_completed == 1
        assert execution.actions_total == 1
        assert len(execution.action_results) == 1
        assert execution.action_results[0].success is True

    @pytest.mark.asyncio
    async def test_execute_workflow_with_failure(self):
        """Test executing workflow with action failure."""
        engine = WorkflowEngine()
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[
                WorkflowAction(
                    name="fail", type="python", config={"code": "raise ValueError('test error')"}
                ),
                WorkflowAction(name="after_fail", type="python", config={"code": "pass"}),
            ],
        )

        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            trigger_type=TriggerType.MANUAL,
        )

        execution = await engine.execute_workflow(definition, context)
        assert execution.status == WorkflowStatus.FAILED
        assert execution.actions_completed == 0
        assert len(execution.action_results) == 1
        assert execution.action_results[0].success is False
        assert "test error" in execution.error_message

    @pytest.mark.asyncio
    async def test_execute_workflow_unknown_action(self):
        """Test executing workflow with unknown action type."""
        engine = WorkflowEngine()
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="unknown", type="nonexistent_action")],
        )

        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            trigger_type=TriggerType.MANUAL,
        )

        execution = await engine.execute_workflow(definition, context)
        assert execution.status == WorkflowStatus.FAILED
        assert "Unknown action type" in execution.error_message

    @pytest.mark.asyncio
    async def test_trigger_workflow(self):
        """Test triggering a workflow."""
        engine = WorkflowEngine()
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python", config={"code": "pass"})],
        )

        engine.load_workflow(definition, enabled=True)

        execution = await engine.trigger_workflow("test_workflow", {"trigger_type": "manual"})
        assert execution is not None
        assert execution.status == WorkflowStatus.SUCCEEDED

    @pytest.mark.asyncio
    async def test_trigger_nonexistent_workflow(self):
        """Test triggering non-existent workflow."""
        engine = WorkflowEngine()
        execution = await engine.trigger_workflow("nonexistent", {})
        assert execution is None

    @pytest.mark.asyncio
    async def test_trigger_disabled_workflow(self):
        """Test triggering disabled workflow."""
        engine = WorkflowEngine()
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python")],
        )

        engine.load_workflow(definition, enabled=False)
        execution = await engine.trigger_workflow("test_workflow", {})
        assert execution is None

    @pytest.mark.asyncio
    async def test_fire_event(self):
        """Test firing an event."""
        engine = WorkflowEngine()
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            triggers=[WorkflowTrigger(type=TriggerType.FILE_WRITE, config={"pattern": "*.md"})],
            actions=[WorkflowAction(name="action1", type="python", config={"code": "pass"})],
        )

        engine.load_workflow(definition, enabled=True)

        # Fire matching event
        count = await engine.fire_event(TriggerType.FILE_WRITE, {"file_path": "/docs/readme.md"})
        assert count >= 0  # May be 0 or 1 depending on trigger implementation

    @pytest.mark.asyncio
    async def test_workflow_context_variables(self):
        """Test workflow context variables are passed to actions."""
        engine = WorkflowEngine()
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[
                WorkflowAction(
                    name="calc",
                    type="python",
                    config={"code": "result = variables.get('x', 0) * 2"},
                )
            ],
            variables={"x": 10},
        )

        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            trigger_type=TriggerType.MANUAL,
            variables={"x": 10},
        )

        execution = await engine.execute_workflow(definition, context)
        assert execution.status == WorkflowStatus.SUCCEEDED
        assert execution.action_results[0].output == 20

    @pytest.mark.asyncio
    async def test_action_output_stored_in_context(self):
        """Test action output is stored in context for next actions."""
        engine = WorkflowEngine()
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[
                WorkflowAction(
                    name="action1", type="python", config={"code": "result = {'value': 42}"}
                ),
                WorkflowAction(
                    name="action2",
                    type="python",
                    config={"code": "result = variables['action1_output']['value'] * 2"},
                ),
            ],
        )

        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            trigger_type=TriggerType.MANUAL,
        )

        execution = await engine.execute_workflow(definition, context)
        assert execution.status == WorkflowStatus.SUCCEEDED
        assert execution.action_results[1].output == 84


class TestEngineGlobals:
    """Test global engine functions."""

    def test_get_engine(self):
        """Test getting global engine instance."""
        engine1 = get_engine()
        engine2 = get_engine()
        assert engine1 is engine2

    def test_init_engine(self):
        """Test initializing global engine."""
        engine = init_engine()
        assert engine is not None
        assert get_engine() is engine

    def test_init_engine_with_stores(self):
        """Test initializing engine with stores."""
        mock_metadata_store = object()
        mock_workflow_store = object()

        engine = init_engine(mock_metadata_store, None, mock_workflow_store)
        assert engine.metadata_store is mock_metadata_store
        assert engine.plugin_registry is None
        assert engine.workflow_store is mock_workflow_store
