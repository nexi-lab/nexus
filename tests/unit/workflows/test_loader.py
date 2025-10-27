"""Tests for workflow loader."""

import tempfile
from pathlib import Path

import pytest

from nexus.workflows.loader import WorkflowLoader
from nexus.workflows.types import TriggerType, WorkflowAction, WorkflowDefinition, WorkflowTrigger


class TestWorkflowLoader:
    """Test WorkflowLoader."""

    def test_load_from_dict_minimal(self):
        """Test loading minimal workflow from dict."""
        data = {"name": "test_workflow", "actions": [{"name": "action1", "type": "python"}]}

        definition = WorkflowLoader.load_from_dict(data)
        assert definition.name == "test_workflow"
        assert definition.version == "1.0"
        assert len(definition.actions) == 1
        assert definition.actions[0].name == "action1"

    def test_load_from_dict_complete(self):
        """Test loading complete workflow from dict."""
        data = {
            "name": "test_workflow",
            "version": "2.0",
            "description": "A test workflow",
            "variables": {"env": "test"},
            "triggers": [{"type": "file_write", "pattern": "*.md"}],
            "actions": [
                {"name": "action1", "type": "parse", "parser": "markdown"},
                {"name": "action2", "type": "tag", "tags": ["processed"]},
            ],
        }

        definition = WorkflowLoader.load_from_dict(data)
        assert definition.name == "test_workflow"
        assert definition.version == "2.0"
        assert definition.description == "A test workflow"
        assert definition.variables == {"env": "test"}
        assert len(definition.triggers) == 1
        assert definition.triggers[0].type == TriggerType.FILE_WRITE
        assert len(definition.actions) == 2

    def test_load_from_dict_no_name(self):
        """Test loading workflow without name fails."""
        data = {"actions": [{"name": "action1", "type": "python"}]}

        with pytest.raises(ValueError, match="must have a 'name'"):
            WorkflowLoader.load_from_dict(data)

    def test_load_from_dict_no_actions(self):
        """Test loading workflow without actions fails."""
        data = {"name": "test_workflow", "actions": []}

        with pytest.raises(ValueError, match="at least one action"):
            WorkflowLoader.load_from_dict(data)

    def test_load_from_dict_invalid_type(self):
        """Test loading workflow with invalid type fails."""
        with pytest.raises(ValueError, match="must be a dictionary"):
            WorkflowLoader.load_from_dict("not a dict")

    def test_load_from_dict_invalid_triggers(self):
        """Test loading workflow with invalid triggers format."""
        data = {
            "name": "test_workflow",
            "triggers": "not a list",
            "actions": [{"name": "a", "type": "python"}],
        }

        with pytest.raises(ValueError, match="must be a list"):
            WorkflowLoader.load_from_dict(data)

    def test_load_from_dict_invalid_actions(self):
        """Test loading workflow with invalid actions format."""
        data = {"name": "test_workflow", "actions": "not a list"}

        with pytest.raises(ValueError, match="must be a list"):
            WorkflowLoader.load_from_dict(data)

    def test_load_from_string(self):
        """Test loading workflow from YAML string."""
        yaml_string = """
name: test_workflow
version: '1.0'
actions:
  - name: action1
    type: python
    code: pass
"""

        definition = WorkflowLoader.load_from_string(yaml_string)
        assert definition.name == "test_workflow"
        assert len(definition.actions) == 1

    def test_load_from_file(self):
        """Test loading workflow from file."""
        yaml_content = """
name: test_workflow
version: '1.0'
description: Test workflow from file
actions:
  - name: action1
    type: python
    code: pass
"""

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name

        try:
            definition = WorkflowLoader.load_from_file(temp_path)
            assert definition.name == "test_workflow"
            assert definition.description == "Test workflow from file"
        finally:
            Path(temp_path).unlink()

    def test_load_from_file_not_found(self):
        """Test loading from non-existent file fails."""
        with pytest.raises(FileNotFoundError):
            WorkflowLoader.load_from_file("/nonexistent/file.yaml")

    def test_parse_trigger_valid(self):
        """Test parsing valid trigger."""
        data = {"type": "file_write", "pattern": "*.md"}
        trigger = WorkflowLoader._parse_trigger(data)

        assert trigger is not None
        assert trigger.type == TriggerType.FILE_WRITE
        assert trigger.config == {"pattern": "*.md"}

    def test_parse_trigger_no_type(self):
        """Test parsing trigger without type."""
        data = {"pattern": "*.md"}
        trigger = WorkflowLoader._parse_trigger(data)
        assert trigger is None

    def test_parse_trigger_invalid_type(self):
        """Test parsing trigger with invalid type."""
        data = {"type": "invalid_type"}
        trigger = WorkflowLoader._parse_trigger(data)
        assert trigger is None

    def test_parse_trigger_invalid_data(self):
        """Test parsing trigger with invalid data."""
        trigger = WorkflowLoader._parse_trigger("not a dict")
        assert trigger is None

    def test_parse_action_valid(self):
        """Test parsing valid action."""
        data = {"name": "test_action", "type": "python", "code": "pass"}
        action = WorkflowLoader._parse_action(data)

        assert action is not None
        assert action.name == "test_action"
        assert action.type == "python"
        assert action.config == {"code": "pass"}

    def test_parse_action_no_name(self):
        """Test parsing action without name."""
        data = {"type": "python"}
        action = WorkflowLoader._parse_action(data)
        assert action is None

    def test_parse_action_no_type(self):
        """Test parsing action without type."""
        data = {"name": "test_action"}
        action = WorkflowLoader._parse_action(data)
        assert action is None

    def test_parse_action_invalid_data(self):
        """Test parsing action with invalid data."""
        action = WorkflowLoader._parse_action("not a dict")
        assert action is None

    def test_save_to_file(self):
        """Test saving workflow to file."""
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            description="Test workflow",
            variables={"env": "test"},
            triggers=[WorkflowTrigger(type=TriggerType.FILE_WRITE, config={"pattern": "*.md"})],
            actions=[WorkflowAction(name="action1", type="python", config={"code": "pass"})],
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "test_workflow.yaml"
            WorkflowLoader.save_to_file(definition, file_path)

            # Verify file was created
            assert file_path.exists()

            # Load it back and verify
            loaded = WorkflowLoader.load_from_file(file_path)
            assert loaded.name == definition.name
            assert loaded.version == definition.version
            assert loaded.description == definition.description
            assert len(loaded.triggers) == 1
            assert len(loaded.actions) == 1

    def test_save_to_file_creates_directory(self):
        """Test saving workflow creates parent directories."""
        definition = WorkflowDefinition(
            name="test_workflow",
            version="1.0",
            actions=[WorkflowAction(name="action1", type="python")],
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "subdir" / "workflows" / "test.yaml"
            WorkflowLoader.save_to_file(definition, file_path)

            assert file_path.exists()
            assert file_path.parent.exists()

    def test_discover_workflows(self):
        """Test discovering workflows in directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            dir_path = Path(temp_dir)

            # Create test workflows
            workflow1 = """
name: workflow1
version: '1.0'
actions:
  - name: action1
    type: python
"""
            workflow2 = """
name: workflow2
version: '2.0'
actions:
  - name: action1
    type: bash
"""

            (dir_path / "workflow1.yaml").write_text(workflow1)
            (dir_path / "workflow2.yml").write_text(workflow2)

            # Discover workflows
            workflows = WorkflowLoader.discover_workflows(dir_path)
            assert len(workflows) == 2

            names = {w.name for w in workflows}
            assert "workflow1" in names
            assert "workflow2" in names

    def test_discover_workflows_nested(self):
        """Test discovering workflows in nested directories."""
        with tempfile.TemporaryDirectory() as temp_dir:
            dir_path = Path(temp_dir)

            # Create nested directories
            (dir_path / "level1" / "level2").mkdir(parents=True)

            # Create workflows at different levels
            workflow1 = """
name: workflow1
version: '1.0'
actions:
  - name: action1
    type: python
"""
            workflow2 = """
name: workflow2
version: '2.0'
actions:
  - name: action1
    type: bash
"""

            (dir_path / "workflow1.yaml").write_text(workflow1)
            (dir_path / "level1" / "level2" / "workflow2.yaml").write_text(workflow2)

            # Discover workflows
            workflows = WorkflowLoader.discover_workflows(dir_path)
            assert len(workflows) == 2

    def test_discover_workflows_nonexistent_directory(self):
        """Test discovering workflows in non-existent directory."""
        workflows = WorkflowLoader.discover_workflows("/nonexistent/directory")
        assert workflows == []

    def test_discover_workflows_with_invalid(self):
        """Test discovering workflows with some invalid files."""
        with tempfile.TemporaryDirectory() as temp_dir:
            dir_path = Path(temp_dir)

            # Create valid workflow
            valid = """
name: valid_workflow
version: '1.0'
actions:
  - name: action1
    type: python
"""
            # Create invalid workflow (missing actions)
            invalid = """
name: invalid_workflow
version: '1.0'
"""

            (dir_path / "valid.yaml").write_text(valid)
            (dir_path / "invalid.yaml").write_text(invalid)

            # Should only discover valid workflow
            workflows = WorkflowLoader.discover_workflows(dir_path)
            assert len(workflows) == 1
            assert workflows[0].name == "valid_workflow"
