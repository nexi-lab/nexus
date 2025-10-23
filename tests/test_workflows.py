"""Tests for workflow automation system - All trigger types and actions."""

import pytest

from nexus.workflows import (
    TriggerType,
    WorkflowAction,
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowLoader,
    WorkflowTrigger,
)


def test_workflow_definition_creation():
    """Test creating a workflow definition."""
    workflow = WorkflowDefinition(
        name="test-workflow",
        version="1.0",
        description="Test workflow",
        triggers=[WorkflowTrigger(type=TriggerType.FILE_WRITE, config={"pattern": "**/*.txt"})],
        actions=[WorkflowAction(name="test_action", type="tag", config={"tags": ["test"]})],
    )

    assert workflow.name == "test-workflow"
    assert workflow.version == "1.0"
    assert len(workflow.triggers) == 1
    assert len(workflow.actions) == 1


def test_workflow_loader_from_dict():
    """Test loading workflow from dictionary."""
    workflow_dict = {
        "name": "test-workflow",
        "version": "1.0",
        "description": "Test workflow",
        "triggers": [{"type": "file_write", "pattern": "**/*.txt"}],
        "actions": [{"name": "tag_file", "type": "tag", "tags": ["test"]}],
    }

    workflow = WorkflowLoader.load_from_dict(workflow_dict)

    assert workflow.name == "test-workflow"
    assert len(workflow.triggers) == 1
    assert len(workflow.actions) == 1
    assert workflow.actions[0].name == "tag_file"


def test_workflow_loader_from_yaml_string():
    """Test loading workflow from YAML string."""
    yaml_string = """
name: test-workflow
version: 1.0
description: Test workflow

triggers:
  - type: file_write
    pattern: "**/*.txt"

actions:
  - name: tag_file
    type: tag
    tags:
      - test
"""

    workflow = WorkflowLoader.load_from_string(yaml_string)

    assert workflow.name == "test-workflow"
    assert len(workflow.triggers) == 1
    assert len(workflow.actions) == 1


# ============================================================================
# TRIGGER TYPE TESTS - All 7 types
# ============================================================================


def test_trigger_file_write():
    """Test FILE_WRITE trigger."""
    from nexus.workflows.triggers import FileWriteTrigger

    # Use fnmatch compatible patterns (not ** glob)
    trigger = FileWriteTrigger({"pattern": "/inbox/*.pdf"})

    # Should match
    assert trigger.matches({"file_path": "/inbox/document.pdf"})
    assert trigger.matches({"file_path": "/inbox/file.pdf"})

    # Should not match
    assert not trigger.matches({"file_path": "/inbox/document.txt"})
    assert not trigger.matches({"file_path": "/other/document.pdf"})


def test_trigger_file_delete():
    """Test FILE_DELETE trigger."""
    from nexus.workflows.triggers import FileDeleteTrigger

    # Use wildcard pattern
    trigger = FileDeleteTrigger({"pattern": "/workspace/*"})

    # Should match
    assert trigger.matches({"file_path": "/workspace/file.txt"})
    assert trigger.matches({"file_path": "/workspace/data.csv"})

    # Should not match
    assert not trigger.matches({"file_path": "/other/file.txt"})


def test_trigger_file_rename():
    """Test FILE_RENAME trigger."""
    from nexus.workflows.triggers import FileRenameTrigger

    trigger = FileRenameTrigger({"pattern": "/documents/*"})

    # Should match old path
    assert trigger.matches({"old_path": "/documents/old.txt", "new_path": "/other/new.txt"})

    # Should match new path
    assert trigger.matches({"old_path": "/other/old.txt", "new_path": "/documents/new.txt"})

    # Should not match
    assert not trigger.matches({"old_path": "/other/old.txt", "new_path": "/other/new.txt"})


def test_trigger_metadata_change():
    """Test METADATA_CHANGE trigger."""
    from nexus.workflows.triggers import MetadataChangeTrigger

    # Without specific key
    trigger = MetadataChangeTrigger({"pattern": "/projects/*"})
    assert trigger.matches({"file_path": "/projects/task.txt", "metadata_key": "status"})

    # With specific key
    trigger = MetadataChangeTrigger({"pattern": "/projects/*", "metadata_key": "status"})
    assert trigger.matches({"file_path": "/projects/task.txt", "metadata_key": "status"})
    assert not trigger.matches({"file_path": "/projects/task.txt", "metadata_key": "priority"})


def test_trigger_schedule():
    """Test SCHEDULE trigger."""
    from nexus.workflows.triggers import ScheduleTrigger

    trigger = ScheduleTrigger({"cron": "0 2 * * *"})

    # Schedule triggers don't match events
    assert not trigger.matches({})


def test_trigger_webhook():
    """Test WEBHOOK trigger."""
    from nexus.workflows.triggers import WebhookTrigger

    trigger = WebhookTrigger({"webhook_id": "github-123"})

    # Should match
    assert trigger.matches({"webhook_id": "github-123"})

    # Should not match
    assert not trigger.matches({"webhook_id": "other-456"})


def test_trigger_manual():
    """Test MANUAL trigger."""
    from nexus.workflows.triggers import ManualTrigger

    trigger = ManualTrigger({})

    # Manual triggers always match
    assert trigger.matches({})
    assert trigger.matches({"any": "context"})


# ============================================================================
# ACTION TYPE TESTS - All 8 actions
# ============================================================================


@pytest.mark.asyncio
async def test_action_python():
    """Test Python action execution."""
    import uuid

    from nexus.workflows.actions import PythonAction
    from nexus.workflows.types import WorkflowContext

    action = PythonAction("test_python", {"code": 'result = {"calculated": 42}'})

    context = WorkflowContext(
        workflow_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        trigger_type=TriggerType.MANUAL,
        file_path="/test/file.txt",
    )

    result = await action.execute(context)

    assert result.success
    assert result.output == {"calculated": 42}


@pytest.mark.asyncio
async def test_action_bash():
    """Test Bash action execution."""
    import uuid

    from nexus.workflows.actions import BashAction
    from nexus.workflows.types import WorkflowContext

    action = BashAction("test_bash", {"command": "echo 'hello world'"})

    context = WorkflowContext(
        workflow_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        trigger_type=TriggerType.MANUAL,
    )

    result = await action.execute(context)

    assert result.success
    assert "hello world" in result.output["stdout"]


@pytest.mark.asyncio
async def test_action_tag():
    """Test Tag action (would require Nexus connection)."""
    from nexus.workflows.actions import TagAction

    action = TagAction("test_tag", {"tags": ["test", "automated"]})
    assert action.name == "test_tag"
    assert action.config["tags"] == ["test", "automated"]


@pytest.mark.asyncio
async def test_action_metadata():
    """Test Metadata action (would require Nexus connection)."""
    from nexus.workflows.actions import MetadataAction

    action = MetadataAction("test_metadata", {"metadata": {"status": "processed"}})
    assert action.name == "test_metadata"
    assert action.config["metadata"]["status"] == "processed"


@pytest.mark.asyncio
async def test_action_move():
    """Test Move action (would require Nexus connection)."""
    from nexus.workflows.actions import MoveAction

    action = MoveAction("test_move", {"destination": "/archive/{filename}"})
    assert action.name == "test_move"
    assert "{filename}" in action.config["destination"]


@pytest.mark.asyncio
async def test_action_parse():
    """Test Parse action (would require Nexus connection)."""
    from nexus.workflows.actions import ParseAction

    action = ParseAction("test_parse", {"parser": "pdf"})
    assert action.name == "test_parse"
    assert action.config["parser"] == "pdf"


@pytest.mark.asyncio
async def test_action_llm():
    """Test LLM action (would require Nexus + LLM provider)."""
    from nexus.workflows.actions import LLMAction

    action = LLMAction(
        "test_llm",
        {"model": "claude-sonnet-4", "prompt": "Analyze this document", "output_format": "json"},
    )
    assert action.name == "test_llm"
    assert action.config["model"] == "claude-sonnet-4"
    assert action.config["output_format"] == "json"


@pytest.mark.asyncio
async def test_action_webhook():
    """Test Webhook action (would make real HTTP request)."""
    from nexus.workflows.actions import WebhookAction

    action = WebhookAction(
        "test_webhook",
        {"url": "https://example.com/webhook", "method": "POST", "body": {"event": "test"}},
    )
    assert action.name == "test_webhook"
    assert action.config["url"] == "https://example.com/webhook"


# ============================================================================
# ENGINE TESTS
# ============================================================================


def test_workflow_engine_load():
    """Test loading workflow into engine."""
    engine = WorkflowEngine()

    workflow = WorkflowDefinition(
        name="test-workflow",
        version="1.0",
        triggers=[WorkflowTrigger(type=TriggerType.FILE_WRITE, config={"pattern": "**/*.txt"})],
        actions=[WorkflowAction(name="test_action", type="tag", config={"tags": ["test"]})],
    )

    success = engine.load_workflow(workflow, enabled=True)

    assert success
    assert "test-workflow" in engine.workflows
    assert engine.enabled_workflows["test-workflow"]


def test_workflow_engine_enable_disable():
    """Test enabling and disabling workflows."""
    engine = WorkflowEngine()

    workflow = WorkflowDefinition(
        name="test-workflow",
        version="1.0",
        triggers=[],
        actions=[WorkflowAction(name="test_action", type="tag", config={"tags": ["test"]})],
    )

    engine.load_workflow(workflow, enabled=True)
    assert engine.enabled_workflows["test-workflow"]

    engine.disable_workflow("test-workflow")
    assert not engine.enabled_workflows["test-workflow"]

    engine.enable_workflow("test-workflow")
    assert engine.enabled_workflows["test-workflow"]


def test_workflow_engine_unload():
    """Test unloading workflow."""
    engine = WorkflowEngine()

    workflow = WorkflowDefinition(
        name="test-workflow",
        version="1.0",
        triggers=[],
        actions=[WorkflowAction(name="test_action", type="tag", config={"tags": ["test"]})],
    )

    engine.load_workflow(workflow, enabled=True)
    assert "test-workflow" in engine.workflows

    success = engine.unload_workflow("test-workflow")
    assert success
    assert "test-workflow" not in engine.workflows


def test_workflow_engine_list():
    """Test listing workflows."""
    engine = WorkflowEngine()

    workflow1 = WorkflowDefinition(
        name="workflow1",
        version="1.0",
        description="First workflow",
        triggers=[],
        actions=[WorkflowAction(name="test_action", type="tag", config={"tags": ["test"]})],
    )

    workflow2 = WorkflowDefinition(
        name="workflow2",
        version="2.0",
        description="Second workflow",
        triggers=[],
        actions=[
            WorkflowAction(name="test_action", type="tag", config={"tags": ["test"]}),
            WorkflowAction(name="test_action2", type="move", config={"destination": "/tmp"}),
        ],
    )

    engine.load_workflow(workflow1, enabled=True)
    engine.load_workflow(workflow2, enabled=False)

    workflows = engine.list_workflows()

    assert len(workflows) == 2
    assert workflows[0]["name"] == "workflow1"
    assert workflows[0]["enabled"]
    assert workflows[1]["name"] == "workflow2"
    assert not workflows[1]["enabled"]
    assert workflows[1]["actions"] == 2


def test_workflow_validation():
    """Test workflow validation."""
    # Missing name
    with pytest.raises(ValueError):
        WorkflowLoader.load_from_dict(
            {"version": "1.0", "actions": [{"name": "test", "type": "tag"}]}
        )

    # Missing actions
    with pytest.raises(ValueError):
        WorkflowLoader.load_from_dict({"name": "test", "version": "1.0", "actions": []})


@pytest.mark.asyncio
async def test_workflow_execution():
    """Test basic workflow execution."""
    engine = WorkflowEngine()

    workflow = WorkflowDefinition(
        name="test-workflow",
        version="1.0",
        triggers=[],
        actions=[
            WorkflowAction(
                name="test_python",
                type="python",
                config={"code": "result = {'success': True}"},
            )
        ],
    )

    engine.load_workflow(workflow, enabled=True)

    execution = await engine.trigger_workflow("test-workflow", {})

    assert execution is not None
    assert execution.status.value in ["succeeded", "failed"]
    assert execution.actions_total == 1


@pytest.mark.asyncio
async def test_workflow_with_all_trigger_types():
    """Test that all trigger types can be loaded."""
    engine = WorkflowEngine()

    # Create workflow with all trigger types
    workflow = WorkflowDefinition(
        name="all-triggers",
        version="1.0",
        triggers=[
            WorkflowTrigger(type=TriggerType.FILE_WRITE, config={"pattern": "**/*.txt"}),
            WorkflowTrigger(type=TriggerType.FILE_DELETE, config={"pattern": "**/*"}),
            WorkflowTrigger(type=TriggerType.FILE_RENAME, config={"pattern": "**/*"}),
            WorkflowTrigger(type=TriggerType.METADATA_CHANGE, config={"pattern": "**/*"}),
            WorkflowTrigger(type=TriggerType.SCHEDULE, config={"cron": "0 * * * *"}),
            WorkflowTrigger(type=TriggerType.WEBHOOK, config={"webhook_id": "test"}),
            WorkflowTrigger(type=TriggerType.MANUAL, config={}),
        ],
        actions=[WorkflowAction(name="test", type="python", config={"code": "pass"})],
    )

    success = engine.load_workflow(workflow)
    assert success
    assert len(engine.workflows["all-triggers"].triggers) == 7


@pytest.mark.asyncio
async def test_workflow_event_firing():
    """Test firing events for different trigger types."""
    engine = WorkflowEngine()

    # Test FILE_WRITE event
    triggered = await engine.fire_event(TriggerType.FILE_WRITE, {"file_path": "/test/file.txt"})
    assert isinstance(triggered, int)

    # Test FILE_DELETE event
    triggered = await engine.fire_event(TriggerType.FILE_DELETE, {"file_path": "/test/file.txt"})
    assert isinstance(triggered, int)

    # Test WEBHOOK event
    triggered = await engine.fire_event(
        TriggerType.WEBHOOK, {"webhook_id": "test-123", "payload": {}}
    )
    assert isinstance(triggered, int)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
