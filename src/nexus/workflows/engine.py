"""Workflow execution engine."""

import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from nexus.workflows.actions import BUILTIN_ACTIONS
from nexus.workflows.triggers import BUILTIN_TRIGGERS, TriggerManager
from nexus.workflows.types import (
    TriggerType,
    WorkflowContext,
    WorkflowDefinition,
    WorkflowExecution,
    WorkflowStatus,
)

logger = logging.getLogger(__name__)


class WorkflowEngine:
    """Core workflow execution engine."""

    def __init__(self, metadata_store=None, plugin_registry=None, workflow_store=None):  # type: ignore[no-untyped-def]
        self.metadata_store = metadata_store
        self.plugin_registry = plugin_registry
        self.workflow_store = workflow_store
        self.trigger_manager = TriggerManager()
        self.workflows: dict[str, WorkflowDefinition] = {}
        self.enabled_workflows: dict[str, bool] = {}
        self.workflow_ids: dict[str, str] = {}  # name -> workflow_id mapping
        self.action_registry = BUILTIN_ACTIONS.copy()
        self.trigger_registry = BUILTIN_TRIGGERS.copy()

        # Discover plugin actions and triggers
        if plugin_registry:
            self._discover_plugin_extensions()

        # Load workflows from storage if available
        if workflow_store:
            self._load_workflows_from_storage()

    def _discover_plugin_extensions(self) -> None:
        """Discover actions and triggers from plugins."""
        if not self.plugin_registry:
            return

        for plugin in self.plugin_registry.get_enabled_plugins():
            # Discover plugin actions
            if hasattr(plugin, "workflow_actions"):
                plugin_actions = plugin.workflow_actions()
                self.action_registry.update(plugin_actions)
                logger.info(f"Registered {len(plugin_actions)} actions from plugin {plugin.name}")

            # Discover plugin triggers
            if hasattr(plugin, "workflow_triggers"):
                plugin_triggers = plugin.workflow_triggers()
                self.trigger_registry.update(plugin_triggers)
                logger.info(f"Registered {len(plugin_triggers)} triggers from plugin {plugin.name}")

    def _load_workflows_from_storage(self) -> None:
        """Load all workflows from storage."""
        if not self.workflow_store:
            return

        try:
            workflows_list = self.workflow_store.list_workflows()
            for workflow_info in workflows_list:
                definition = self.workflow_store.load_workflow(workflow_info["workflow_id"])
                if definition:
                    self.workflows[definition.name] = definition
                    self.enabled_workflows[definition.name] = workflow_info["enabled"]
                    self.workflow_ids[definition.name] = workflow_info["workflow_id"]

                    # Register triggers
                    for trigger_def in definition.triggers:
                        trigger_class = self.trigger_registry.get(trigger_def.type)
                        if trigger_class:
                            trigger = trigger_class(trigger_def.config)  # type: ignore[abstract]

                            # Bind definition.name to avoid closure issue
                            async def trigger_callback(
                                event_context: dict[str, Any], wf_name: str = definition.name
                            ) -> None:
                                await self.trigger_workflow(wf_name, event_context)

                            self.trigger_manager.register_trigger(trigger, trigger_callback)  # type: ignore[arg-type]

            logger.info(f"Loaded {len(workflows_list)} workflow(s) from storage")
        except Exception as e:
            logger.error(f"Failed to load workflows from storage: {e}")

    def load_workflow(self, definition: WorkflowDefinition, enabled: bool = True) -> bool:
        """Load a workflow definition.

        Args:
            definition: Workflow definition
            enabled: Whether workflow is enabled

        Returns:
            True if loaded successfully
        """
        try:
            # Validate workflow
            if not definition.name:
                raise ValueError("Workflow must have a name")

            if not definition.actions:
                raise ValueError("Workflow must have at least one action")

            # Save to storage if available
            if self.workflow_store:
                workflow_id = self.workflow_store.save_workflow(definition, enabled)
                self.workflow_ids[definition.name] = workflow_id
                logger.info(f"Saved workflow to storage: {definition.name} (id={workflow_id})")

            # Store workflow in memory
            self.workflows[definition.name] = definition
            self.enabled_workflows[definition.name] = enabled

            # Register triggers
            import sys

            print(f"[ENGINE] Registering triggers for workflow: {definition.name}", file=sys.stderr)
            print(f"[ENGINE] Number of triggers: {len(definition.triggers)}", file=sys.stderr)
            print(
                f"[ENGINE] Trigger registry has: {list(self.trigger_registry.keys())}",
                file=sys.stderr,
            )

            for trigger_def in definition.triggers:
                print(
                    f"[ENGINE] Processing trigger_def: type={trigger_def.type}, config={trigger_def.config}",
                    file=sys.stderr,
                )
                trigger_class = self.trigger_registry.get(trigger_def.type)
                print(f"[ENGINE] Got trigger_class: {trigger_class}", file=sys.stderr)
                if not trigger_class:
                    logger.warning(f"Unknown trigger type: {trigger_def.type}, skipping")
                    print(
                        f"[ENGINE] WARNING: Unknown trigger type {trigger_def.type}, skipping",
                        file=sys.stderr,
                    )
                    continue

                trigger = trigger_class(trigger_def.config)  # type: ignore[abstract]
                print(f"[ENGINE] Created trigger instance: {trigger}", file=sys.stderr)

                # Create callback that executes this workflow
                # Bind definition.name to avoid closure issue
                async def trigger_callback(
                    event_context: dict[str, Any], wf_name: str = definition.name
                ) -> None:
                    await self.trigger_workflow(wf_name, event_context)

                print("[ENGINE] Registering trigger with trigger_manager...", file=sys.stderr)
                self.trigger_manager.register_trigger(trigger, trigger_callback)  # type: ignore[arg-type]
                print("[ENGINE] Trigger registered successfully", file=sys.stderr)

            logger.info(f"Loaded workflow: {definition.name} (enabled={enabled})")
            print(f"[ENGINE] Workflow loaded successfully: {definition.name}", file=sys.stderr)
            return True

        except Exception as e:
            logger.error(f"Failed to load workflow {definition.name}: {e}")
            return False

    def unload_workflow(self, name: str) -> bool:
        """Unload a workflow.

        Args:
            name: Workflow name

        Returns:
            True if unloaded successfully
        """
        if name not in self.workflows:
            return False

        # Delete from storage if available
        if self.workflow_store:
            self.workflow_store.delete_workflow_by_name(name)

        # Remove workflow from memory
        del self.workflows[name]
        if name in self.enabled_workflows:
            del self.enabled_workflows[name]
        if name in self.workflow_ids:
            del self.workflow_ids[name]

        logger.info(f"Unloaded workflow: {name}")
        return True

    def enable_workflow(self, name: str) -> None:
        """Enable a workflow.

        Args:
            name: Workflow name
        """
        if name in self.workflows:
            self.enabled_workflows[name] = True

            # Update storage if available
            if self.workflow_store:
                self.workflow_store.set_enabled_by_name(name, True)

            logger.info(f"Enabled workflow: {name}")

    def disable_workflow(self, name: str) -> None:
        """Disable a workflow.

        Args:
            name: Workflow name
        """
        if name in self.workflows:
            self.enabled_workflows[name] = False

            # Update storage if available
            if self.workflow_store:
                self.workflow_store.set_enabled_by_name(name, False)

            logger.info(f"Disabled workflow: {name}")

    def list_workflows(self) -> list[dict[str, Any]]:
        """List all loaded workflows.

        Returns:
            List of workflow info dicts
        """
        result = []
        for name, definition in self.workflows.items():
            result.append(
                {
                    "name": name,
                    "version": definition.version,
                    "description": definition.description,
                    "enabled": self.enabled_workflows.get(name, False),
                    "triggers": len(definition.triggers),
                    "actions": len(definition.actions),
                }
            )
        return result

    async def trigger_workflow(
        self, workflow_name: str, event_context: dict[str, Any]
    ) -> WorkflowExecution | None:
        """Trigger a workflow execution.

        Args:
            workflow_name: Name of workflow to execute
            event_context: Event context data

        Returns:
            WorkflowExecution record or None if failed
        """
        # Check if workflow exists and is enabled
        if workflow_name not in self.workflows:
            logger.warning(f"Workflow not found: {workflow_name}")
            return None

        if not self.enabled_workflows.get(workflow_name, False):
            logger.info(f"Workflow disabled: {workflow_name}")
            return None

        definition = self.workflows[workflow_name]

        # Create execution context
        execution_id = uuid.uuid4()

        # Get the actual workflow_id from storage (stored when workflow was loaded)
        workflow_id_str = self.workflow_ids.get(workflow_name)
        if workflow_id_str:
            # Convert string UUID to UUID object
            workflow_id = uuid.UUID(workflow_id_str)
        else:
            # Fallback: generate a new UUID if not found (shouldn't happen in normal operation)
            workflow_id = uuid.uuid4()
            logger.warning(f"No workflow_id found for {workflow_name}, using generated UUID")

        # Get tenant_id from event context or workflow store
        tenant_id_str = event_context.get("tenant_id", "default")
        try:
            # Try to convert to UUID if it's a valid UUID string
            import uuid as uuid_module

            tenant_id = uuid_module.UUID(tenant_id_str) if tenant_id_str != "default" else None
        except (ValueError, AttributeError):
            # If not a valid UUID, use None or default
            tenant_id = None

        context = WorkflowContext(
            workflow_id=workflow_id,
            execution_id=execution_id,
            tenant_id=tenant_id,
            trigger_type=TriggerType(event_context.get("trigger_type", TriggerType.MANUAL.value)),
            trigger_context=event_context,
            variables=definition.variables.copy(),
            file_path=event_context.get("file_path"),
            file_metadata=event_context.get("metadata"),
        )

        # Execute workflow
        execution = await self.execute_workflow(definition, context)
        return execution

    async def execute_workflow(
        self, definition: WorkflowDefinition, context: WorkflowContext
    ) -> WorkflowExecution:
        """Execute a workflow.

        Args:
            definition: Workflow definition
            context: Execution context

        Returns:
            WorkflowExecution record
        """
        execution = WorkflowExecution(
            execution_id=context.execution_id,
            workflow_id=context.workflow_id,
            workflow_name=definition.name,
            status=WorkflowStatus.RUNNING,
            trigger_type=context.trigger_type,
            trigger_context=context.trigger_context,
            started_at=datetime.now(UTC),
            actions_total=len(definition.actions),
            context={"variables": context.variables},
        )

        logger.info(f"Executing workflow: {definition.name} (execution_id={context.execution_id})")

        import sys

        print(f"[EXECUTE_WORKFLOW] Starting workflow: {definition.name}", file=sys.stderr)
        print(f"[EXECUTE_WORKFLOW] Number of actions: {len(definition.actions)}", file=sys.stderr)

        try:
            # Execute actions sequentially
            for i, action_def in enumerate(definition.actions, 1):
                print(
                    f"[EXECUTE_WORKFLOW] Processing action {i}/{len(definition.actions)}: {action_def.name} (type={action_def.type})",
                    file=sys.stderr,
                )
                action_class = self.action_registry.get(action_def.type)
                print(f"[EXECUTE_WORKFLOW] Got action_class: {action_class}", file=sys.stderr)
                if not action_class:
                    raise ValueError(f"Unknown action type: {action_def.type}")

                # Create action instance
                action = action_class(action_def.name, action_def.config)  # type: ignore[abstract]
                print(f"[EXECUTE_WORKFLOW] Created action instance: {action}", file=sys.stderr)

                # Execute action
                print("[EXECUTE_WORKFLOW] Executing action...", file=sys.stderr)
                start_time = time.time()
                result = await action.execute(context)
                result.duration_ms = (time.time() - start_time) * 1000
                print(
                    f"[EXECUTE_WORKFLOW] Action completed. Success={result.success}, Output={result.output}",
                    file=sys.stderr,
                )

                # Record result
                execution.action_results.append(result)

                if result.success:
                    execution.actions_completed += 1
                    logger.info(
                        f"Action '{action_def.name}' completed successfully in {result.duration_ms:.2f}ms"
                    )

                    # Store action output in context for next actions
                    if result.output:
                        context.variables[f"{action_def.name}_output"] = result.output
                else:
                    # Action failed, stop workflow
                    execution.status = WorkflowStatus.FAILED
                    execution.error_message = f"Action '{action_def.name}' failed: {result.error}"
                    logger.error(execution.error_message)
                    break

            # Workflow completed successfully if we got here
            if execution.status == WorkflowStatus.RUNNING:
                execution.status = WorkflowStatus.SUCCEEDED

        except Exception as e:
            execution.status = WorkflowStatus.FAILED
            execution.error_message = str(e)
            logger.error(f"Workflow execution failed: {e}", exc_info=True)

        finally:
            execution.completed_at = datetime.now(UTC)

        logger.info(f"Workflow '{definition.name}' finished with status: {execution.status}")

        # Store execution record (if metadata store available)
        if self.metadata_store:
            await self._store_execution(execution)

        return execution

    async def _store_execution(self, execution: WorkflowExecution) -> None:
        """Store workflow execution record in database."""
        if not self.workflow_store:
            return

        try:
            self.workflow_store.save_execution(execution)
            logger.info(f"Saved execution record: {execution.execution_id}")
        except Exception as e:
            logger.error(f"Failed to save execution record: {e}")

    async def fire_event(self, trigger_type: TriggerType, event_context: dict[str, Any]) -> int:
        """Fire an event that may trigger workflows.

        Args:
            trigger_type: Type of event
            event_context: Event data

        Returns:
            Number of workflows triggered
        """
        event_context["trigger_type"] = trigger_type.value
        return await self.trigger_manager.fire_event(trigger_type, event_context)


# Global workflow engine instance
_engine: WorkflowEngine | None = None


def get_engine() -> WorkflowEngine:
    """Get the global workflow engine instance."""
    global _engine
    if _engine is None:
        _engine = WorkflowEngine()
    return _engine


def init_engine(metadata_store=None, plugin_registry=None, workflow_store=None) -> WorkflowEngine:  # type: ignore[no-untyped-def]
    """Initialize the workflow engine.

    Args:
        metadata_store: Metadata store instance
        plugin_registry: Plugin registry instance
        workflow_store: Workflow store instance

    Returns:
        Initialized workflow engine
    """
    global _engine
    _engine = WorkflowEngine(metadata_store, plugin_registry, workflow_store)
    return _engine
