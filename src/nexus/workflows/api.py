"""Python SDK API for workflow system."""

import builtins
from pathlib import Path
from typing import Any

from nexus.workflows.engine import WorkflowEngine, get_engine
from nexus.workflows.loader import WorkflowLoader
from nexus.workflows.types import (
    TriggerType,
    WorkflowDefinition,
    WorkflowExecution,
)


class WorkflowAPI:
    """High-level API for workflow management.

    This class provides a clean Python API for managing and executing workflows.
    It wraps the underlying workflow engine with a more user-friendly interface.

    Examples:
        >>> from nexus.workflows import WorkflowAPI
        >>>
        >>> # Create API instance
        >>> workflows = WorkflowAPI()
        >>>
        >>> # Load a workflow
        >>> workflows.load("invoice-processor.yaml")
        >>>
        >>> # List workflows
        >>> for workflow in workflows.list():
        ...     print(f"{workflow['name']}: {workflow['status']}")
        >>>
        >>> # Execute a workflow
        >>> result = workflows.execute("invoice-processor", file_path="/inbox/invoice.pdf")
        >>> print(f"Status: {result.status}")
        >>>
        >>> # Enable/disable workflows
        >>> workflows.enable("invoice-processor")
        >>> workflows.disable("invoice-processor")
    """

    def __init__(self, engine: WorkflowEngine | None = None):
        """Initialize workflow API.

        Args:
            engine: Optional workflow engine instance. If not provided,
                   uses the global engine.
        """
        self.engine = engine or get_engine()

    def load(self, source: str | Path | dict | WorkflowDefinition, enabled: bool = True) -> bool:
        """Load a workflow from a file, dict, or definition.

        Args:
            source: Workflow source:
                - str/Path: Path to YAML file
                - dict: Workflow definition as dictionary
                - WorkflowDefinition: Already parsed definition
            enabled: Whether to enable the workflow after loading

        Returns:
            True if loaded successfully

        Examples:
            >>> workflows.load("process-invoices.yaml")
            >>> workflows.load({"name": "test", "actions": [...]})
        """
        # Parse source into WorkflowDefinition
        if isinstance(source, WorkflowDefinition):
            definition = source
        elif isinstance(source, dict):
            definition = WorkflowLoader.load_from_dict(source)
        else:
            definition = WorkflowLoader.load_from_file(source)

        return self.engine.load_workflow(definition, enabled=enabled)

    def list(self) -> list[dict[str, Any]]:
        """List all loaded workflows.

        Returns:
            List of workflow info dictionaries with keys:
                - name: Workflow name
                - version: Workflow version
                - description: Workflow description
                - enabled: Whether workflow is enabled
                - triggers: Number of triggers
                - actions: Number of actions

        Examples:
            >>> for workflow in workflows.list():
            ...     print(f"{workflow['name']}: {workflow['enabled']}")
        """
        return self.engine.list_workflows()

    def get(self, name: str) -> WorkflowDefinition | None:
        """Get a workflow definition by name.

        Args:
            name: Workflow name

        Returns:
            WorkflowDefinition or None if not found

        Examples:
            >>> workflow = workflows.get("invoice-processor")
            >>> if workflow:
            ...     print(f"Actions: {len(workflow.actions)}")
        """
        return self.engine.workflows.get(name)

    async def execute(
        self,
        name: str,
        file_path: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> WorkflowExecution | None:
        """Execute a workflow manually.

        Args:
            name: Workflow name
            file_path: Optional file path to process
            context: Optional additional context

        Returns:
            WorkflowExecution record or None if failed

        Examples:
            >>> result = await workflows.execute(
            ...     "invoice-processor",
            ...     file_path="/inbox/invoice.pdf"
            ... )
            >>> print(f"Status: {result.status}")
            >>> print(f"Actions completed: {result.actions_completed}")
        """
        event_context = context or {}
        if file_path:
            event_context["file_path"] = file_path

        return await self.engine.trigger_workflow(name, event_context)

    def enable(self, name: str) -> bool:
        """Enable a workflow.

        Args:
            name: Workflow name

        Returns:
            True if workflow exists and was enabled

        Examples:
            >>> workflows.enable("invoice-processor")
        """
        if name in self.engine.workflows:
            self.engine.enable_workflow(name)
            return True
        return False

    def disable(self, name: str) -> bool:
        """Disable a workflow.

        Args:
            name: Workflow name

        Returns:
            True if workflow exists and was disabled

        Examples:
            >>> workflows.disable("invoice-processor")
        """
        if name in self.engine.workflows:
            self.engine.disable_workflow(name)
            return True
        return False

    def unload(self, name: str) -> bool:
        """Unload a workflow.

        Args:
            name: Workflow name

        Returns:
            True if workflow was unloaded

        Examples:
            >>> workflows.unload("invoice-processor")
        """
        return self.engine.unload_workflow(name)

    def discover(
        self, directory: str | Path, load: bool = False
    ) -> builtins.list[WorkflowDefinition]:
        """Discover workflows in a directory.

        Args:
            directory: Directory to search for workflow files
            load: Whether to load discovered workflows

        Returns:
            List of discovered workflow definitions

        Examples:
            >>> discovered = workflows.discover(".nexus/workflows")
            >>> print(f"Found {len(discovered)} workflows")
            >>>
            >>> # Load all discovered workflows
            >>> workflows.discover(".nexus/workflows", load=True)
        """
        definitions = WorkflowLoader.discover_workflows(directory)

        if load:
            for definition in definitions:
                self.engine.load_workflow(definition, enabled=True)

        return definitions

    async def fire_event(self, trigger_type: TriggerType, event_context: dict[str, Any]) -> int:
        """Fire an event that may trigger workflows.

        Args:
            trigger_type: Type of event (FILE_WRITE, FILE_DELETE, etc.)
            event_context: Event data

        Returns:
            Number of workflows triggered

        Examples:
            >>> # Trigger file write event
            >>> count = await workflows.fire_event(
            ...     TriggerType.FILE_WRITE,
            ...     {"file_path": "/inbox/document.pdf"}
            ... )
            >>> print(f"Triggered {count} workflows")
        """
        return await self.engine.fire_event(trigger_type, event_context)

    def is_enabled(self, name: str) -> bool:
        """Check if a workflow is enabled.

        Args:
            name: Workflow name

        Returns:
            True if workflow exists and is enabled

        Examples:
            >>> if workflows.is_enabled("invoice-processor"):
            ...     print("Workflow is active")
        """
        return self.engine.enabled_workflows.get(name, False)

    def get_status(self, name: str) -> str | None:
        """Get the status of a workflow.

        Args:
            name: Workflow name

        Returns:
            "enabled", "disabled", or None if not found

        Examples:
            >>> status = workflows.get_status("invoice-processor")
            >>> print(f"Workflow status: {status}")
        """
        if name not in self.engine.workflows:
            return None
        return "enabled" if self.is_enabled(name) else "disabled"


# Convenience function for quick access
def get_workflow_api() -> WorkflowAPI:
    """Get a WorkflowAPI instance.

    This is a convenience function for quick access to workflow functionality.

    Returns:
        WorkflowAPI instance

    Examples:
        >>> from nexus.workflows import get_workflow_api
        >>>
        >>> workflows = get_workflow_api()
        >>> workflows.load("process-invoices.yaml")
    """
    return WorkflowAPI()
