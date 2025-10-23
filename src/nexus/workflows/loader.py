"""YAML workflow definition loader."""

import logging
from pathlib import Path
from typing import Any

import yaml

from nexus.workflows.types import (
    TriggerType,
    WorkflowAction,
    WorkflowDefinition,
    WorkflowTrigger,
)

logger = logging.getLogger(__name__)


class WorkflowLoader:
    """Load workflow definitions from YAML files."""

    @staticmethod
    def load_from_file(file_path: str | Path) -> WorkflowDefinition:
        """Load a workflow from a YAML file.

        Args:
            file_path: Path to YAML file

        Returns:
            WorkflowDefinition

        Raises:
            FileNotFoundError: If file doesn't exist
            ValueError: If YAML is invalid
        """
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"Workflow file not found: {file_path}")

        with open(file_path) as f:
            yaml_content = yaml.safe_load(f)

        return WorkflowLoader.load_from_dict(yaml_content)

    @staticmethod
    def load_from_string(yaml_string: str) -> WorkflowDefinition:
        """Load a workflow from a YAML string.

        Args:
            yaml_string: YAML content as string

        Returns:
            WorkflowDefinition

        Raises:
            ValueError: If YAML is invalid
        """
        yaml_content = yaml.safe_load(yaml_string)
        return WorkflowLoader.load_from_dict(yaml_content)

    @staticmethod
    def load_from_dict(data: dict[str, Any]) -> WorkflowDefinition:
        """Load a workflow from a dictionary.

        Args:
            data: Workflow definition as dict

        Returns:
            WorkflowDefinition

        Raises:
            ValueError: If definition is invalid
        """
        if not isinstance(data, dict):
            raise ValueError("Workflow definition must be a dictionary")

        # Required fields
        name = data.get("name")
        if not name:
            raise ValueError("Workflow must have a 'name' field")

        version = str(data.get("version", "1.0"))

        # Optional fields
        description = str(data.get("description", "") or "")
        variables = data.get("variables", {})

        # Parse triggers
        triggers = []
        triggers_data = data.get("triggers", [])
        if not isinstance(triggers_data, list):
            raise ValueError("'triggers' must be a list")

        for trigger_data in triggers_data:
            trigger = WorkflowLoader._parse_trigger(trigger_data)
            if trigger:
                triggers.append(trigger)

        # Parse actions
        actions = []
        actions_data = data.get("actions", [])
        if not isinstance(actions_data, list):
            raise ValueError("'actions' must be a list")

        if not actions_data:
            raise ValueError("Workflow must have at least one action")

        for action_data in actions_data:
            action = WorkflowLoader._parse_action(action_data)
            if action:
                actions.append(action)

        return WorkflowDefinition(
            name=name,
            version=version,
            description=description,
            triggers=triggers,
            actions=actions,
            variables=variables,
        )

    @staticmethod
    def _parse_trigger(data: dict[str, Any]) -> WorkflowTrigger | None:
        """Parse a trigger definition.

        Args:
            data: Trigger data

        Returns:
            WorkflowTrigger or None if invalid
        """
        if not isinstance(data, dict):
            logger.warning(f"Invalid trigger data: {data}")
            return None

        trigger_type_str = data.get("type")
        if not trigger_type_str:
            logger.warning("Trigger missing 'type' field")
            return None

        # Convert string to TriggerType enum
        try:
            trigger_type = TriggerType(trigger_type_str)
        except ValueError:
            logger.warning(f"Unknown trigger type: {trigger_type_str}")
            return None

        # Extract config (everything except 'type')
        config = {k: v for k, v in data.items() if k != "type"}

        return WorkflowTrigger(type=trigger_type, config=config)

    @staticmethod
    def _parse_action(data: dict[str, Any]) -> WorkflowAction | None:
        """Parse an action definition.

        Args:
            data: Action data

        Returns:
            WorkflowAction or None if invalid
        """
        if not isinstance(data, dict):
            logger.warning(f"Invalid action data: {data}")
            return None

        action_name = data.get("name")
        if not action_name:
            logger.warning("Action missing 'name' field")
            return None

        action_type = data.get("type")
        if not action_type:
            logger.warning("Action missing 'type' field")
            return None

        # Extract config (everything except 'name' and 'type')
        config = {k: v for k, v in data.items() if k not in ["name", "type"]}

        return WorkflowAction(name=action_name, type=action_type, config=config)

    @staticmethod
    def save_to_file(definition: WorkflowDefinition, file_path: str | Path) -> None:
        """Save a workflow definition to a YAML file.

        Args:
            definition: Workflow definition
            file_path: Output file path
        """
        file_path = Path(file_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Convert to dict
        data: dict[str, Any] = {
            "name": definition.name,
            "version": definition.version,
            "description": definition.description,
        }

        if definition.variables:
            data["variables"] = definition.variables

        # Add triggers
        if definition.triggers:
            data["triggers"] = []
            for trigger in definition.triggers:
                trigger_dict = {"type": trigger.type.value, **trigger.config}
                data["triggers"].append(trigger_dict)

        # Add actions
        data["actions"] = []
        for action in definition.actions:
            action_dict = {"name": action.name, "type": action.type, **action.config}
            data["actions"].append(action_dict)

        # Write YAML
        with open(file_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

        logger.info(f"Saved workflow definition to {file_path}")

    @staticmethod
    def discover_workflows(directory: str | Path) -> list[WorkflowDefinition]:
        """Discover and load all workflows in a directory.

        Args:
            directory: Directory to search

        Returns:
            List of workflow definitions
        """
        directory = Path(directory)
        if not directory.exists():
            logger.warning(f"Workflow directory not found: {directory}")
            return []

        workflows = []
        for yaml_file in directory.glob("**/*.yaml"):
            try:
                workflow = WorkflowLoader.load_from_file(yaml_file)
                workflows.append(workflow)
                logger.info(f"Discovered workflow: {workflow.name} from {yaml_file}")
            except Exception as e:
                logger.error(f"Failed to load workflow from {yaml_file}: {e}")

        for yml_file in directory.glob("**/*.yml"):
            try:
                workflow = WorkflowLoader.load_from_file(yml_file)
                workflows.append(workflow)
                logger.info(f"Discovered workflow: {workflow.name} from {yml_file}")
            except Exception as e:
                logger.error(f"Failed to load workflow from {yml_file}: {e}")

        return workflows
