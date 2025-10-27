"""Tests for workflow actions."""

import uuid

import pytest

from nexus.workflows.actions import (
    BUILTIN_ACTIONS,
    BaseAction,
    BashAction,
    MetadataAction,
    MoveAction,
    PythonAction,
    TagAction,
    WebhookAction,
)
from nexus.workflows.types import TriggerType, WorkflowContext


class MockAction(BaseAction):
    """Mock action for testing base class."""

    async def execute(self, context: WorkflowContext):
        """Execute mock action."""
        return {"success": True}


class TestBaseAction:
    """Test BaseAction base class."""

    def test_create_action(self):
        """Test creating base action."""
        action = MockAction(name="test", config={"key": "value"})
        assert action.name == "test"
        assert action.config == {"key": "value"}

    def test_interpolate_simple(self):
        """Test simple variable interpolation."""
        action = MockAction(name="test", config={})
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            trigger_type=TriggerType.MANUAL,
            variables={"name": "world"},
        )

        result = action.interpolate("Hello {name}!", context)
        assert result == "Hello world!"

    def test_interpolate_file_path(self):
        """Test interpolating file path variables."""
        action = MockAction(name="test", config={})
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            trigger_type=TriggerType.FILE_WRITE,
            file_path="/docs/readme.md",
        )

        assert action.interpolate("{file_path}", context) == "/docs/readme.md"
        assert action.interpolate("{filename}", context) == "readme.md"
        assert action.interpolate("{dirname}", context) == "/docs"

    def test_interpolate_file_metadata(self):
        """Test interpolating file metadata."""
        action = MockAction(name="test", config={})
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            trigger_type=TriggerType.FILE_WRITE,
            file_path="/docs/readme.md",
            file_metadata={"author": "test_user", "size": 1024},
        )

        assert action.interpolate("Author: {author}", context) == "Author: test_user"
        assert action.interpolate("Size: {size}", context) == "Size: 1024"

    def test_interpolate_missing_variable(self):
        """Test interpolating missing variable."""
        action = MockAction(name="test", config={})
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            trigger_type=TriggerType.MANUAL,
            variables={},
        )

        # Should return original string if variable not found
        result = action.interpolate("Hello {missing}!", context)
        assert result == "Hello {missing}!"

    def test_interpolate_non_string(self):
        """Test interpolating non-string value."""
        action = MockAction(name="test", config={})
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            trigger_type=TriggerType.MANUAL,
        )

        # Should return value unchanged if not string
        assert action.interpolate(123, context) == 123
        assert action.interpolate(True, context) is True


class TestTagAction:
    """Test TagAction."""

    def test_create_tag_action(self):
        """Test creating tag action."""
        action = TagAction(name="add_tag", config={"tags": ["important", "reviewed"]})
        assert action.name == "add_tag"
        assert action.config["tags"] == ["important", "reviewed"]


class TestMoveAction:
    """Test MoveAction."""

    def test_create_move_action(self):
        """Test creating move action."""
        action = MoveAction(
            name="move_file", config={"source": "/old/path.txt", "destination": "/new/path.txt"}
        )
        assert action.name == "move_file"
        assert action.config["source"] == "/old/path.txt"
        assert action.config["destination"] == "/new/path.txt"

    def test_interpolate_paths(self):
        """Test interpolating move action paths."""
        action = MoveAction(
            name="move_file",
            config={"source": "{file_path}", "destination": "/archive/{filename}"},
        )
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            trigger_type=TriggerType.FILE_WRITE,
            file_path="/docs/readme.md",
        )

        source = action.interpolate(action.config["source"], context)
        destination = action.interpolate(action.config["destination"], context)

        assert source == "/docs/readme.md"
        assert destination == "/archive/readme.md"


class TestMetadataAction:
    """Test MetadataAction."""

    def test_create_metadata_action(self):
        """Test creating metadata action."""
        action = MetadataAction(
            name="set_metadata", config={"metadata": {"status": "processed", "version": "1.0"}}
        )
        assert action.name == "set_metadata"
        assert action.config["metadata"] == {"status": "processed", "version": "1.0"}


class TestPythonAction:
    """Test PythonAction."""

    def test_create_python_action(self):
        """Test creating Python action."""
        code = "result = 2 + 2"
        action = PythonAction(name="calc", config={"code": code})
        assert action.name == "calc"
        assert action.config["code"] == code

    @pytest.mark.asyncio
    async def test_execute_python_action(self):
        """Test executing Python action."""
        code = "result = variables['x'] + variables['y']"
        action = PythonAction(name="calc", config={"code": code})
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            trigger_type=TriggerType.MANUAL,
            variables={"x": 10, "y": 20},
        )

        result = await action.execute(context)
        assert result.success is True
        assert result.output == 30

    @pytest.mark.asyncio
    async def test_execute_python_action_error(self):
        """Test executing Python action with error."""
        code = "raise ValueError('test error')"
        action = PythonAction(name="calc", config={"code": code})
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            trigger_type=TriggerType.MANUAL,
        )

        result = await action.execute(context)
        assert result.success is False
        assert "test error" in result.error


class TestBashAction:
    """Test BashAction."""

    def test_create_bash_action(self):
        """Test creating bash action."""
        action = BashAction(name="list_files", config={"command": "ls -la"})
        assert action.name == "list_files"
        assert action.config["command"] == "ls -la"

    @pytest.mark.asyncio
    async def test_execute_bash_action(self):
        """Test executing bash action."""
        action = BashAction(name="echo_test", config={"command": "echo 'hello world'"})
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            trigger_type=TriggerType.MANUAL,
        )

        result = await action.execute(context)
        assert result.success is True
        assert "hello world" in result.output["stdout"]

    @pytest.mark.asyncio
    async def test_execute_bash_action_error(self):
        """Test executing bash action with error."""
        action = BashAction(name="fail", config={"command": "exit 1"})
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            trigger_type=TriggerType.MANUAL,
        )

        result = await action.execute(context)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_bash_action_interpolation(self):
        """Test bash action with variable interpolation."""
        action = BashAction(name="echo_var", config={"command": "echo {greeting}"})
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            trigger_type=TriggerType.MANUAL,
            variables={"greeting": "hello"},
        )

        result = await action.execute(context)
        assert result.success is True
        assert "hello" in result.output["stdout"]


class TestWebhookAction:
    """Test WebhookAction."""

    def test_create_webhook_action(self):
        """Test creating webhook action."""
        action = WebhookAction(
            name="notify",
            config={
                "url": "https://example.com/webhook",
                "method": "POST",
                "body": {"status": "done"},
            },
        )
        assert action.name == "notify"
        assert action.config["url"] == "https://example.com/webhook"
        assert action.config["method"] == "POST"


class TestBuiltinActions:
    """Test built-in action registry."""

    def test_all_actions_registered(self):
        """Test all action types are in registry."""
        assert "parse" in BUILTIN_ACTIONS
        assert "tag" in BUILTIN_ACTIONS
        assert "move" in BUILTIN_ACTIONS
        assert "metadata" in BUILTIN_ACTIONS
        assert "llm" in BUILTIN_ACTIONS
        assert "webhook" in BUILTIN_ACTIONS
        assert "python" in BUILTIN_ACTIONS
        assert "bash" in BUILTIN_ACTIONS

    def test_action_classes(self):
        """Test action classes are correct type."""
        for _action_type, action_class in BUILTIN_ACTIONS.items():
            assert issubclass(action_class, BaseAction)
