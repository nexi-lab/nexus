"""Python SDK API for workflow system.

No global engine — the engine must be explicitly provided.
"""

import builtins
from pathlib import Path
from typing import Any

from nexus.workflows.engine import WorkflowEngine
from nexus.workflows.loader import WorkflowLoader
from nexus.workflows.types import (
    TriggerType,
    WorkflowDefinition,
    WorkflowExecution,
)

class WorkflowAPI:
    """High-level API for workflow management.

    Examples:
        >>> from nexus.workflows import WorkflowAPI
        >>>
        >>> workflows = WorkflowAPI(engine=engine)
        >>> workflows.load("invoice-processor.yaml")
        >>> for workflow in workflows.list():
        ...     print(f"{workflow['name']}: enabled={workflow['enabled']}")
    """

    def __init__(self, engine: WorkflowEngine) -> None:
        """Initialize workflow API.

        Args:
            engine: Workflow engine instance (required).
        """
        self.engine = engine

    def load(self, source: str | Path | dict | WorkflowDefinition, enabled: bool = True) -> bool:
        """Load a workflow from a file, dict, or definition."""
        if isinstance(source, WorkflowDefinition):
            definition = source
        elif isinstance(source, dict):
            definition = WorkflowLoader.load_from_dict(source)
        else:
            definition = WorkflowLoader.load_from_file(source)

        return self.engine.load_workflow(definition, enabled=enabled)

    def list(self) -> list[dict[str, Any]]:
        """List all loaded workflows."""
        return self.engine.list_workflows()

    def get(self, name: str) -> WorkflowDefinition | None:
        """Get a workflow definition by name."""
        return self.engine.workflows.get(name)

    async def execute(
        self,
        name: str,
        file_path: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> WorkflowExecution | None:
        """Execute a workflow manually."""
        event_context = context or {}
        if file_path:
            event_context["file_path"] = file_path

        return await self.engine.trigger_workflow(name, event_context)

    def enable(self, name: str) -> bool:
        """Enable a workflow."""
        if name in self.engine.workflows:
            self.engine.enable_workflow(name)
            return True
        return False

    def disable(self, name: str) -> bool:
        """Disable a workflow."""
        if name in self.engine.workflows:
            self.engine.disable_workflow(name)
            return True
        return False

    def unload(self, name: str) -> bool:
        """Unload a workflow."""
        return self.engine.unload_workflow(name)

    def discover(
        self, directory: str | Path, load: bool = False
    ) -> builtins.list[WorkflowDefinition]:
        """Discover workflows in a directory."""
        definitions = WorkflowLoader.discover_workflows(directory)

        if load:
            for definition in definitions:
                self.engine.load_workflow(definition, enabled=True)

        return definitions

    async def fire_event(
        self, trigger_type: TriggerType | str, event_context: dict[str, Any]
    ) -> int:
        """Fire an event that may trigger workflows."""
        return await self.engine.fire_event(trigger_type, event_context)

    def is_enabled(self, name: str) -> bool:
        """Check if a workflow is enabled."""
        return self.engine.enabled_workflows.get(name, False)

    def get_status(self, name: str) -> str | None:
        """Get the status of a workflow."""
        if name not in self.engine.workflows:
            return None
        return "enabled" if self.is_enabled(name) else "disabled"
