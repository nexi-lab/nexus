"""Tests for workflow actions (Issue #1756 hardened)."""

import uuid
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.workflows.actions import (
    BUILTIN_ACTIONS,
    BaseAction,
    BashAction,
    MetadataAction,
    MoveAction,
    PythonAction,
    TagAction,
    WebhookAction,
)
from nexus.bricks.workflows.types import TriggerType, WorkflowContext


@dataclass
class _MockCodeResult:
    """Mock sandbox code execution result."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    execution_time: float = 0.1


def _make_sandbox_services(*, stdout: str = "", stderr: str = "", exit_code: int = 0):
    """Create mock services with a sandbox_manager for PythonAction tests."""
    mock_sandbox_mgr = AsyncMock()
    mock_sandbox_mgr.get_or_create_sandbox.return_value = {"sandbox_id": "sb-test"}
    mock_sandbox_mgr.run_code.return_value = _MockCodeResult(
        stdout=stdout, stderr=stderr, exit_code=exit_code
    )
    mock_services = MagicMock()
    mock_services.sandbox_manager = mock_sandbox_mgr
    return mock_services


class MockAction(BaseAction):
    """Mock action for testing base class."""

    async def execute(self, context: WorkflowContext):
        """Execute mock action."""
        return {"success": True}


class TestBaseAction:
    """Test BaseAction base class."""

    def test_create_action(self):
        action = MockAction(name="test", config={"key": "value"})
        assert action.name == "test"
        assert action.config == {"key": "value"}

    def test_interpolate_simple(self):
        action = MockAction(name="test", config={})
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            zone_id="test-zone",
            trigger_type=TriggerType.MANUAL,
            variables={"name": "world"},
        )
        result = action.safe_interpolate("Hello {name}!", context)
        assert result == "Hello world!"

    def test_interpolate_file_path(self):
        action = MockAction(name="test", config={})
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            zone_id="test-zone",
            trigger_type=TriggerType.FILE_WRITE,
            file_path="/docs/readme.md",
        )
        assert action.safe_interpolate("{file_path}", context) == "/docs/readme.md"
        assert action.safe_interpolate("{filename}", context) == "readme.md"
        assert action.safe_interpolate("{dirname}", context) == "/docs"

    def test_interpolate_file_metadata(self):
        action = MockAction(name="test", config={})
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            zone_id="test-zone",
            trigger_type=TriggerType.FILE_WRITE,
            file_path="/docs/readme.md",
            file_metadata={"author": "test_user", "size": 1024},
        )
        assert action.safe_interpolate("Author: {author}", context) == "Author: test_user"
        assert action.safe_interpolate("Size: {size}", context) == "Size: 1024"

    def test_interpolate_missing_variable(self):
        action = MockAction(name="test", config={})
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            zone_id="test-zone",
            trigger_type=TriggerType.MANUAL,
            variables={},
        )
        result = action.safe_interpolate("Hello {missing}!", context)
        assert result == "Hello {missing}!"

    def test_interpolate_non_string(self):
        action = MockAction(name="test", config={})
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            zone_id="test-zone",
            trigger_type=TriggerType.MANUAL,
        )
        assert action.safe_interpolate(123, context) == 123
        assert action.safe_interpolate(True, context) is True


class TestTagAction:
    """Test TagAction."""

    def test_create_tag_action(self):
        action = TagAction(name="add_tag", config={"tags": ["important", "reviewed"]})
        assert action.name == "add_tag"
        assert action.config["tags"] == ["important", "reviewed"]

    @pytest.mark.asyncio
    async def test_tag_action_no_services(self):
        action = TagAction(name="add_tag", config={"tags": ["important"]})
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            zone_id="test-zone",
            trigger_type=TriggerType.FILE_WRITE,
            file_path="/docs/readme.md",
            services=None,
        )
        result = await action.execute(context)
        assert result.success is False
        assert "not injected" in result.error or "not available" in result.error


class TestMoveAction:
    """Test MoveAction."""

    def test_create_move_action(self):
        action = MoveAction(
            name="move_file", config={"source": "/old/path.txt", "destination": "/new/path.txt"}
        )
        assert action.name == "move_file"

    def test_interpolate_paths(self):
        action = MoveAction(
            name="move_file",
            config={"source": "{file_path}", "destination": "/archive/{filename}"},
        )
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            zone_id="test-zone",
            trigger_type=TriggerType.FILE_WRITE,
            file_path="/docs/readme.md",
        )
        source = action.safe_interpolate(action.config["source"], context)
        destination = action.safe_interpolate(action.config["destination"], context)
        assert source == "/docs/readme.md"
        assert destination == "/archive/readme.md"

    @pytest.mark.asyncio
    async def test_move_action_no_services(self):
        action = MoveAction(
            name="move_file", config={"source": "/old/path.txt", "destination": "/new/path.txt"}
        )
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            zone_id="test-zone",
            trigger_type=TriggerType.FILE_WRITE,
            file_path="/old/path.txt",
            services=None,
        )
        result = await action.execute(context)
        assert result.success is False
        assert "not injected" in result.error or "not available" in result.error


class TestMetadataAction:
    """Test MetadataAction."""

    def test_create_metadata_action(self):
        action = MetadataAction(
            name="set_metadata", config={"metadata": {"status": "processed", "version": "1.0"}}
        )
        assert action.name == "set_metadata"

    @pytest.mark.asyncio
    async def test_metadata_action_no_services(self):
        action = MetadataAction(name="set_metadata", config={"metadata": {"status": "done"}})
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            zone_id="test-zone",
            trigger_type=TriggerType.FILE_WRITE,
            file_path="/docs/readme.md",
            services=None,
        )
        result = await action.execute(context)
        assert result.success is False
        assert "not injected" in result.error or "not available" in result.error


class TestPythonAction:
    """Test PythonAction (requires sandbox)."""

    def test_create_python_action(self):
        code = "result = 2 + 2"
        action = PythonAction(name="calc", config={"code": code})
        assert action.name == "calc"
        assert action.config["code"] == code

    @pytest.mark.asyncio
    async def test_execute_python_action(self):
        code = "result = variables['x'] + variables['y']"
        action = PythonAction(name="calc", config={"code": code})
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            zone_id="test-zone",
            trigger_type=TriggerType.MANUAL,
            variables={"x": 10, "y": 20},
            services=_make_sandbox_services(stdout="30"),
        )
        result = await action.execute(context)
        assert result.success is True
        assert result.output["stdout"] == "30"

    @pytest.mark.asyncio
    async def test_execute_python_action_error(self):
        code = "raise ValueError('test error')"
        action = PythonAction(name="calc", config={"code": code})
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            zone_id="test-zone",
            trigger_type=TriggerType.MANUAL,
            services=_make_sandbox_services(stderr="test error", exit_code=1),
        )
        result = await action.execute(context)
        assert result.success is False
        assert "test error" in result.error


class TestBashAction:
    """Test BashAction (Issue #1756: requires SandboxManager)."""

    def test_create_bash_action(self):
        action = BashAction(name="list_files", config={"command": "ls -la"})
        assert action.name == "list_files"
        assert action.config["command"] == "ls -la"

    @pytest.mark.asyncio
    async def test_execute_bash_action(self):
        action = BashAction(name="echo_test", config={"command": "echo 'hello world'"})
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            zone_id="test-zone",
            trigger_type=TriggerType.MANUAL,
            services=_make_sandbox_services(stdout="hello world\n"),
        )
        result = await action.execute(context)
        assert result.success is True
        assert "hello world" in result.output["stdout"]

    @pytest.mark.asyncio
    async def test_execute_bash_action_error(self):
        action = BashAction(name="fail", config={"command": "exit 1"})
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            zone_id="test-zone",
            trigger_type=TriggerType.MANUAL,
            services=_make_sandbox_services(stderr="", exit_code=1),
        )
        result = await action.execute(context)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_bash_action_no_sandbox_fails_closed(self):
        action = BashAction(name="echo_test", config={"command": "echo hello"})
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            zone_id="test-zone",
            trigger_type=TriggerType.MANUAL,
        )
        result = await action.execute(context)
        assert result.success is False
        assert "sandbox" in result.error.lower()

    @pytest.mark.asyncio
    async def test_bash_action_interpolation(self):
        action = BashAction(name="echo_var", config={"command": "echo {greeting}"})
        context = WorkflowContext(
            workflow_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            zone_id="test-zone",
            trigger_type=TriggerType.MANUAL,
            variables={"greeting": "hello"},
            services=_make_sandbox_services(stdout="hello\n"),
        )
        result = await action.execute(context)
        assert result.success is True
        assert "hello" in result.output["stdout"]


class TestWebhookAction:
    """Test WebhookAction."""

    def test_create_webhook_action(self):
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
        assert "parse" in BUILTIN_ACTIONS
        assert "tag" in BUILTIN_ACTIONS
        assert "move" in BUILTIN_ACTIONS
        assert "metadata" in BUILTIN_ACTIONS
        assert "webhook" in BUILTIN_ACTIONS
        assert "python" in BUILTIN_ACTIONS
        assert "bash" in BUILTIN_ACTIONS

    def test_action_classes(self):
        for _action_type, action_class in BUILTIN_ACTIONS.items():
            assert issubclass(action_class, BaseAction)
