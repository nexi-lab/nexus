"""Tests for workflow API."""

import tempfile
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from nexus.workflows.api import WorkflowAPI, get_workflow_api
from nexus.workflows.engine import WorkflowEngine
from nexus.workflows.types import (
    TriggerType,
    WorkflowAction,
    WorkflowDefinition,
    WorkflowExecution,
    WorkflowStatus,
    WorkflowTrigger,
)


@pytest.fixture
def mock_engine():
    """Create a mock workflow engine."""
    engine = Mock(spec=WorkflowEngine)
    engine.workflows = {}
    engine.enabled_workflows = {}
    engine.list_workflows.return_value = []
    engine.load_workflow.return_value = True
    engine.unload_workflow.return_value = True
    return engine


@pytest.fixture
def workflow_api(mock_engine):
    """Create a workflow API instance with mock engine."""
    return WorkflowAPI(engine=mock_engine)


@pytest.fixture
def sample_workflow():
    """Create a sample workflow definition."""
    return WorkflowDefinition(
        name="test_workflow",
        version="1.0",
        description="Test workflow",
        triggers=[WorkflowTrigger(type=TriggerType.FILE_WRITE, config={"pattern": "*.txt"})],
        actions=[WorkflowAction(name="test_action", type="parse")],
        variables={"env": "test"},
    )


class TestWorkflowAPIInit:
    """Test WorkflowAPI initialization."""

    def test_init_with_engine(self, mock_engine):
        """Test initializing with provided engine."""
        api = WorkflowAPI(engine=mock_engine)
        assert api.engine == mock_engine

    @patch("nexus.workflows.api.get_engine")
    def test_init_without_engine(self, mock_get_engine):
        """Test initializing without engine uses global engine."""
        mock_global_engine = Mock()
        mock_get_engine.return_value = mock_global_engine

        api = WorkflowAPI()
        assert api.engine == mock_global_engine
        mock_get_engine.assert_called_once()


class TestLoad:
    """Test loading workflows."""

    def test_load_from_definition(self, workflow_api, mock_engine, sample_workflow):
        """Test loading from WorkflowDefinition."""
        result = workflow_api.load(sample_workflow)

        assert result is True
        mock_engine.load_workflow.assert_called_once_with(sample_workflow, enabled=True)

    @patch("nexus.workflows.api.WorkflowLoader")
    def test_load_from_dict(self, mock_loader, workflow_api, mock_engine, sample_workflow):
        """Test loading from dictionary."""
        workflow_dict = {
            "name": "test_workflow",
            "version": "1.0",
            "actions": [{"name": "test", "type": "parse"}],
        }
        mock_loader.load_from_dict.return_value = sample_workflow

        result = workflow_api.load(workflow_dict)

        assert result is True
        mock_loader.load_from_dict.assert_called_once_with(workflow_dict)
        mock_engine.load_workflow.assert_called_once_with(sample_workflow, enabled=True)

    @patch("nexus.workflows.api.WorkflowLoader")
    def test_load_from_file(self, mock_loader, workflow_api, mock_engine, sample_workflow):
        """Test loading from file path."""
        file_path = Path("/test/workflow.yaml")
        mock_loader.load_from_file.return_value = sample_workflow

        result = workflow_api.load(file_path)

        assert result is True
        mock_loader.load_from_file.assert_called_once_with(file_path)
        mock_engine.load_workflow.assert_called_once_with(sample_workflow, enabled=True)

    def test_load_disabled(self, workflow_api, mock_engine, sample_workflow):
        """Test loading workflow in disabled state."""
        workflow_api.load(sample_workflow, enabled=False)

        mock_engine.load_workflow.assert_called_once_with(sample_workflow, enabled=False)


class TestList:
    """Test listing workflows."""

    def test_list_empty(self, workflow_api, mock_engine):
        """Test listing when no workflows loaded."""
        mock_engine.list_workflows.return_value = []

        workflows = workflow_api.list()

        assert workflows == []
        mock_engine.list_workflows.assert_called_once()

    def test_list_workflows(self, workflow_api, mock_engine):
        """Test listing loaded workflows."""
        expected = [
            {
                "name": "workflow1",
                "version": "1.0",
                "description": "First workflow",
                "enabled": True,
                "triggers": 1,
                "actions": 2,
            },
            {
                "name": "workflow2",
                "version": "2.0",
                "description": "Second workflow",
                "enabled": False,
                "triggers": 2,
                "actions": 3,
            },
        ]
        mock_engine.list_workflows.return_value = expected

        workflows = workflow_api.list()

        assert len(workflows) == 2
        assert workflows[0]["name"] == "workflow1"
        assert workflows[1]["name"] == "workflow2"


class TestGet:
    """Test getting workflow by name."""

    def test_get_existing_workflow(self, workflow_api, mock_engine, sample_workflow):
        """Test getting an existing workflow."""
        mock_engine.workflows = {"test_workflow": sample_workflow}

        result = workflow_api.get("test_workflow")

        assert result == sample_workflow

    def test_get_nonexistent_workflow(self, workflow_api, mock_engine):
        """Test getting a non-existent workflow."""
        mock_engine.workflows = {}

        result = workflow_api.get("nonexistent")

        assert result is None


class TestExecute:
    """Test executing workflows."""

    @pytest.mark.asyncio
    async def test_execute_with_file_path(self, workflow_api, mock_engine):
        """Test executing workflow with file path."""
        execution = WorkflowExecution(
            execution_id=uuid.uuid4(),
            workflow_id=uuid.uuid4(),
            workflow_name="test_workflow",
            status=WorkflowStatus.SUCCEEDED,
            trigger_type=TriggerType.MANUAL,
            trigger_context={},
        )
        mock_engine.trigger_workflow = AsyncMock(return_value=execution)

        result = await workflow_api.execute("test_workflow", file_path="/test/file.txt")

        assert result == execution
        mock_engine.trigger_workflow.assert_called_once_with(
            "test_workflow", {"file_path": "/test/file.txt"}
        )

    @pytest.mark.asyncio
    async def test_execute_with_context(self, workflow_api, mock_engine):
        """Test executing workflow with custom context."""
        execution = WorkflowExecution(
            execution_id=uuid.uuid4(),
            workflow_id=uuid.uuid4(),
            workflow_name="test_workflow",
            status=WorkflowStatus.SUCCEEDED,
            trigger_type=TriggerType.MANUAL,
            trigger_context={},
        )
        mock_engine.trigger_workflow = AsyncMock(return_value=execution)
        context = {"custom": "data", "env": "test"}

        result = await workflow_api.execute("test_workflow", context=context)

        assert result == execution
        mock_engine.trigger_workflow.assert_called_once_with("test_workflow", context)

    @pytest.mark.asyncio
    async def test_execute_with_file_and_context(self, workflow_api, mock_engine):
        """Test executing workflow with both file path and context."""
        execution = WorkflowExecution(
            execution_id=uuid.uuid4(),
            workflow_id=uuid.uuid4(),
            workflow_name="test_workflow",
            status=WorkflowStatus.SUCCEEDED,
            trigger_type=TriggerType.MANUAL,
            trigger_context={},
        )
        mock_engine.trigger_workflow = AsyncMock(return_value=execution)

        result = await workflow_api.execute(
            "test_workflow", file_path="/test/file.txt", context={"env": "prod"}
        )

        assert result == execution
        # File path should be merged into context
        expected_context = {"env": "prod", "file_path": "/test/file.txt"}
        mock_engine.trigger_workflow.assert_called_once_with("test_workflow", expected_context)

    @pytest.mark.asyncio
    async def test_execute_failed(self, workflow_api, mock_engine):
        """Test executing workflow that returns None (failed)."""
        mock_engine.trigger_workflow = AsyncMock(return_value=None)

        result = await workflow_api.execute("test_workflow")

        assert result is None


class TestEnable:
    """Test enabling workflows."""

    def test_enable_existing_workflow(self, workflow_api, mock_engine, sample_workflow):
        """Test enabling an existing workflow."""
        mock_engine.workflows = {"test_workflow": sample_workflow}

        result = workflow_api.enable("test_workflow")

        assert result is True
        mock_engine.enable_workflow.assert_called_once_with("test_workflow")

    def test_enable_nonexistent_workflow(self, workflow_api, mock_engine):
        """Test enabling a non-existent workflow."""
        mock_engine.workflows = {}

        result = workflow_api.enable("nonexistent")

        assert result is False
        mock_engine.enable_workflow.assert_not_called()


class TestDisable:
    """Test disabling workflows."""

    def test_disable_existing_workflow(self, workflow_api, mock_engine, sample_workflow):
        """Test disabling an existing workflow."""
        mock_engine.workflows = {"test_workflow": sample_workflow}

        result = workflow_api.disable("test_workflow")

        assert result is True
        mock_engine.disable_workflow.assert_called_once_with("test_workflow")

    def test_disable_nonexistent_workflow(self, workflow_api, mock_engine):
        """Test disabling a non-existent workflow."""
        mock_engine.workflows = {}

        result = workflow_api.disable("nonexistent")

        assert result is False
        mock_engine.disable_workflow.assert_not_called()


class TestUnload:
    """Test unloading workflows."""

    def test_unload_workflow(self, workflow_api, mock_engine):
        """Test unloading a workflow."""
        mock_engine.unload_workflow.return_value = True

        result = workflow_api.unload("test_workflow")

        assert result is True
        mock_engine.unload_workflow.assert_called_once_with("test_workflow")


class TestDiscover:
    """Test discovering workflows."""

    @patch("nexus.workflows.api.WorkflowLoader")
    def test_discover_without_loading(self, mock_loader, workflow_api, sample_workflow):
        """Test discovering workflows without loading them."""
        workflow2 = WorkflowDefinition(name="workflow2", version="1.0")
        mock_loader.discover_workflows.return_value = [sample_workflow, workflow2]

        with tempfile.TemporaryDirectory() as tmpdir:
            discovered = workflow_api.discover(tmpdir, load=False)

        assert len(discovered) == 2
        assert discovered[0] == sample_workflow
        assert discovered[1] == workflow2
        mock_loader.discover_workflows.assert_called_once_with(tmpdir)

    @patch("nexus.workflows.api.WorkflowLoader")
    def test_discover_with_loading(self, mock_loader, workflow_api, mock_engine, sample_workflow):
        """Test discovering and loading workflows."""
        workflow2 = WorkflowDefinition(name="workflow2", version="1.0")
        mock_loader.discover_workflows.return_value = [sample_workflow, workflow2]

        with tempfile.TemporaryDirectory() as tmpdir:
            discovered = workflow_api.discover(tmpdir, load=True)

        assert len(discovered) == 2
        assert mock_engine.load_workflow.call_count == 2
        mock_engine.load_workflow.assert_any_call(sample_workflow, enabled=True)
        mock_engine.load_workflow.assert_any_call(workflow2, enabled=True)


class TestFireEvent:
    """Test firing events."""

    @pytest.mark.asyncio
    async def test_fire_event(self, workflow_api, mock_engine):
        """Test firing an event."""
        mock_engine.fire_event = AsyncMock(return_value=2)
        event_context = {"file_path": "/test/file.txt"}

        count = await workflow_api.fire_event(TriggerType.FILE_WRITE, event_context)

        assert count == 2
        mock_engine.fire_event.assert_called_once_with(TriggerType.FILE_WRITE, event_context)


class TestIsEnabled:
    """Test checking if workflow is enabled."""

    def test_is_enabled_true(self, workflow_api, mock_engine):
        """Test checking enabled workflow."""
        mock_engine.enabled_workflows = {"test_workflow": True}

        result = workflow_api.is_enabled("test_workflow")

        assert result is True

    def test_is_enabled_false(self, workflow_api, mock_engine):
        """Test checking disabled workflow."""
        mock_engine.enabled_workflows = {"test_workflow": False}

        result = workflow_api.is_enabled("test_workflow")

        assert result is False

    def test_is_enabled_nonexistent(self, workflow_api, mock_engine):
        """Test checking non-existent workflow."""
        mock_engine.enabled_workflows = {}

        result = workflow_api.is_enabled("nonexistent")

        assert result is False


class TestGetStatus:
    """Test getting workflow status."""

    def test_get_status_enabled(self, workflow_api, mock_engine, sample_workflow):
        """Test getting status of enabled workflow."""
        mock_engine.workflows = {"test_workflow": sample_workflow}
        mock_engine.enabled_workflows = {"test_workflow": True}

        status = workflow_api.get_status("test_workflow")

        assert status == "enabled"

    def test_get_status_disabled(self, workflow_api, mock_engine, sample_workflow):
        """Test getting status of disabled workflow."""
        mock_engine.workflows = {"test_workflow": sample_workflow}
        mock_engine.enabled_workflows = {"test_workflow": False}

        status = workflow_api.get_status("test_workflow")

        assert status == "disabled"

    def test_get_status_nonexistent(self, workflow_api, mock_engine):
        """Test getting status of non-existent workflow."""
        mock_engine.workflows = {}

        status = workflow_api.get_status("nonexistent")

        assert status is None


class TestGetWorkflowAPI:
    """Test convenience function."""

    @patch("nexus.workflows.api.WorkflowAPI")
    def test_get_workflow_api(self, mock_api_class):
        """Test getting workflow API instance."""
        mock_instance = Mock()
        mock_api_class.return_value = mock_instance

        api = get_workflow_api()

        assert api == mock_instance
        mock_api_class.assert_called_once()
