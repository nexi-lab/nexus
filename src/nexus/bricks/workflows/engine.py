"""Workflow execution engine.

No global singletons. Engine is constructed via DI and wired by factory.py.
"""

import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from cachetools import LRUCache

from nexus.bricks.workflows.actions import BUILTIN_ACTIONS
from nexus.bricks.workflows.protocol import WorkflowServices
from nexus.bricks.workflows.triggers import (
    BUILTIN_TRIGGERS,
    BaseTrigger,
    TriggerFactory,
    TriggerManager,
)
from nexus.bricks.workflows.types import (
    TriggerType,
    WorkflowContext,
    WorkflowDefinition,
    WorkflowExecution,
    WorkflowStatus,
)
from nexus.contracts.constants import ROOT_ZONE_ID

logger = logging.getLogger(__name__)


class WorkflowEngine:
    """Core workflow execution engine."""

    def __init__(
        self,
        *,
        workflow_store: Any | None = None,
        services: WorkflowServices | None = None,
        plugin_registry: Any | None = None,
        max_workflows: int = 1024,
    ) -> None:
        self.workflow_store = workflow_store
        self._services = services
        self.plugin_registry = plugin_registry
        glob_match = services.glob_match if services else None
        self.trigger_manager = TriggerManager(glob_match=glob_match)
        self.workflows: LRUCache[str, WorkflowDefinition] = LRUCache(maxsize=max_workflows)
        self.enabled_workflows: LRUCache[str, bool] = LRUCache(maxsize=max_workflows)
        self.workflow_ids: LRUCache[str, str] = LRUCache(maxsize=max_workflows)
        self.action_registry = BUILTIN_ACTIONS.copy()
        self.trigger_registry: dict[TriggerType, TriggerFactory] = BUILTIN_TRIGGERS.copy()
        # Track triggers per workflow for proper cleanup on unload/disable
        self._workflow_triggers: dict[str, list["BaseTrigger"]] = {}

        if plugin_registry:
            self._discover_plugin_extensions()

    def _discover_plugin_extensions(self) -> None:
        """Discover actions and triggers from plugins."""
        if not self.plugin_registry:
            return

        for plugin in self.plugin_registry.get_enabled_plugins():
            if hasattr(plugin, "workflow_actions"):
                plugin_actions = plugin.workflow_actions()
                self.action_registry.update(plugin_actions)
                logger.info(
                    "Registered %d actions from plugin %s", len(plugin_actions), plugin.name
                )

            if hasattr(plugin, "workflow_triggers"):
                plugin_triggers = plugin.workflow_triggers()
                self.trigger_registry.update(plugin_triggers)
                logger.info(
                    "Registered %d triggers from plugin %s", len(plugin_triggers), plugin.name
                )

    async def startup(self) -> None:
        """Load workflows from storage (must be called post-construction in async context)."""
        if not self.workflow_store:
            return

        try:
            workflows_list = await self.workflow_store.list_workflows()
            for workflow_info in workflows_list:
                definition = await self.workflow_store.load_workflow(
                    workflow_id=workflow_info["workflow_id"]
                )
                if definition:
                    self.workflows[definition.name] = definition
                    self.enabled_workflows[definition.name] = workflow_info["enabled"]
                    self.workflow_ids[definition.name] = workflow_info["workflow_id"]

                    self._register_triggers_for(definition)

            logger.info("Loaded %d workflow(s) from storage", len(workflows_list))
        except Exception as e:
            logger.error("Failed to load workflows from storage: %s", e)

    def _register_triggers_for(self, definition: WorkflowDefinition) -> None:
        """Register triggers for a workflow definition."""
        # Unregister any existing triggers first to prevent duplicates on reload
        self._unregister_triggers_for(definition.name)

        glob_match = self._services.glob_match if self._services else None
        triggers: list[BaseTrigger] = []
        for trigger_def in definition.triggers:
            trigger_class = self.trigger_registry.get(trigger_def.type)
            if not trigger_class:
                logger.warning("Unknown trigger type: %s, skipping", trigger_def.type)
                continue

            trigger = trigger_class(trigger_def.config, glob_match=glob_match)
            triggers.append(trigger)

            async def trigger_callback(
                event_context: dict[str, Any], wf_name: str = definition.name
            ) -> None:
                await self.trigger_workflow(wf_name, event_context)

            self.trigger_manager.register_trigger(trigger, trigger_callback)  # type: ignore[arg-type]

        self._workflow_triggers[definition.name] = triggers

    def _unregister_triggers_for(self, name: str) -> None:
        """Unregister all triggers belonging to a workflow."""
        triggers = self._workflow_triggers.pop(name, [])
        for trigger in triggers:
            self.trigger_manager.unregister_trigger(trigger)

    def load_workflow(self, definition: WorkflowDefinition, *, enabled: bool = True) -> bool:
        """Load a workflow definition."""
        try:
            if not definition.name:
                raise ValueError("Workflow must have a name")

            if not definition.actions:
                raise ValueError("Workflow must have at least one action")

            # Save to storage if available (async store — fire and forget in sync context)
            if self.workflow_store:
                import asyncio

                try:
                    loop = asyncio.get_running_loop()
                    # We're in an async context — create a task
                    task = loop.create_task(self._save_workflow_async(definition, enabled))
                    # Store reference to prevent GC
                    task.add_done_callback(lambda t: t.result() if not t.cancelled() else None)
                except RuntimeError:
                    # No running loop — run synchronously
                    asyncio.run(self._save_workflow_async(definition, enabled))

            # Store workflow in memory
            self.workflows[definition.name] = definition
            self.enabled_workflows[definition.name] = enabled

            # Register triggers
            logger.debug("Registering triggers for workflow: %s", definition.name)
            self._register_triggers_for(definition)

            logger.info("Loaded workflow: %s (enabled=%s)", definition.name, enabled)
            return True

        except Exception as e:
            logger.error("Failed to load workflow %s: %s", definition.name, e)
            return False

    async def _save_workflow_async(self, definition: WorkflowDefinition, enabled: bool) -> None:
        """Save workflow to store (async helper)."""
        try:
            workflow_id = await self.workflow_store.save_workflow(definition, enabled)  # type: ignore[union-attr]
            self.workflow_ids[definition.name] = workflow_id
            logger.info("Saved workflow to storage: %s (id=%s)", definition.name, workflow_id)
        except Exception as e:
            logger.error("Failed to save workflow to storage: %s", e)

    def unload_workflow(self, name: str) -> bool:
        """Unload a workflow."""
        if name not in self.workflows:
            return False

        if self.workflow_store:
            import asyncio

            try:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self.workflow_store.delete_workflow(name=name))
                except RuntimeError:
                    asyncio.run(self.workflow_store.delete_workflow(name=name))
            except Exception as e:
                logger.error("Failed to delete workflow from storage: %s", e)

        # Unregister triggers before removing the workflow
        self._unregister_triggers_for(name)

        del self.workflows[name]
        self.enabled_workflows.pop(name, None)
        self.workflow_ids.pop(name, None)

        logger.info("Unloaded workflow: %s", name)
        return True

    def enable_workflow(self, name: str) -> None:
        """Enable a workflow."""
        if name in self.workflows:
            self.enabled_workflows[name] = True

            # Re-register triggers so events are routed again
            self._register_triggers_for(self.workflows[name])

            if self.workflow_store:
                import asyncio

                try:
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(self.workflow_store.set_enabled(True, name=name))
                    except RuntimeError:
                        asyncio.run(self.workflow_store.set_enabled(True, name=name))
                except Exception as e:
                    logger.error("Failed to persist workflow enable state: %s", e)

            logger.info("Enabled workflow: %s", name)

    def disable_workflow(self, name: str) -> None:
        """Disable a workflow."""
        if name in self.workflows:
            self.enabled_workflows[name] = False

            # Unregister triggers so disabled workflows don't fire
            self._unregister_triggers_for(name)

            if self.workflow_store:
                import asyncio

                try:
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(self.workflow_store.set_enabled(False, name=name))
                    except RuntimeError:
                        asyncio.run(self.workflow_store.set_enabled(False, name=name))
                except Exception as e:
                    logger.error("Failed to persist workflow disable state: %s", e)

            logger.info("Disabled workflow: %s", name)

    def list_workflows(self) -> list[dict[str, Any]]:
        """List all loaded workflows."""
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
        """Trigger a workflow execution."""
        if workflow_name not in self.workflows:
            logger.warning("Workflow not found: %s", workflow_name)
            return None

        if not self.enabled_workflows.get(workflow_name, False):
            logger.info("Workflow disabled: %s", workflow_name)
            return None

        definition = self.workflows[workflow_name]

        execution_id = uuid.uuid4()

        workflow_id_str = self.workflow_ids.get(workflow_name)
        workflow_id = uuid.UUID(workflow_id_str) if workflow_id_str else uuid.uuid4()
        if not workflow_id_str:
            logger.warning("No workflow_id found for %s, using generated UUID", workflow_name)

        zone_id = str(event_context.get("zone_id", ROOT_ZONE_ID))

        context = WorkflowContext(
            workflow_id=workflow_id,
            execution_id=execution_id,
            zone_id=zone_id,
            trigger_type=TriggerType(event_context.get("trigger_type", TriggerType.MANUAL.value)),
            trigger_context=event_context,
            variables=definition.variables.copy(),
            file_path=event_context.get("file_path"),
            file_metadata=event_context.get("metadata"),
            services=self._services,
        )

        execution = await self.execute_workflow(definition, context)
        return execution

    async def execute_workflow(
        self, definition: WorkflowDefinition, context: WorkflowContext
    ) -> WorkflowExecution:
        """Execute a workflow."""
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

        logger.info(
            "Executing workflow: %s (execution_id=%s)", definition.name, context.execution_id
        )
        logger.debug("Number of actions: %d", len(definition.actions))

        try:
            for i, action_def in enumerate(definition.actions, 1):
                logger.debug(
                    "Processing action %d/%d: %s (type=%s)",
                    i,
                    len(definition.actions),
                    action_def.name,
                    action_def.type,
                )
                action_class = self.action_registry.get(action_def.type)
                if not action_class:
                    raise ValueError(f"Unknown action type: {action_def.type}")

                action = action_class(action_def.name, action_def.config)  # type: ignore[abstract]

                start_time = time.time()
                result = await action.execute(context)
                result.duration_ms = (time.time() - start_time) * 1000
                logger.debug(
                    "Action completed: success=%s, duration=%.2fms",
                    result.success,
                    result.duration_ms,
                )

                execution.action_results.append(result)

                if result.success:
                    execution.actions_completed += 1
                    logger.info(
                        "Action '%s' completed successfully in %.2fms",
                        action_def.name,
                        result.duration_ms,
                    )

                    if result.output:
                        context.variables[f"{action_def.name}_output"] = result.output
                else:
                    execution.status = WorkflowStatus.FAILED
                    execution.error_message = f"Action '{action_def.name}' failed: {result.error}"
                    logger.error(execution.error_message)
                    break

            if execution.status == WorkflowStatus.RUNNING:
                execution.status = WorkflowStatus.SUCCEEDED

        except Exception as e:
            execution.status = WorkflowStatus.FAILED
            execution.error_message = str(e)
            logger.error("Workflow execution failed: %s", e, exc_info=True)

        finally:
            execution.completed_at = datetime.now(UTC)

        logger.info("Workflow '%s' finished with status: %s", definition.name, execution.status)

        # Store execution record
        await self._store_execution(execution)

        return execution

    async def _store_execution(self, execution: WorkflowExecution) -> None:
        """Store workflow execution record in database."""
        if not self.workflow_store:
            return

        try:
            await self.workflow_store.save_execution(execution)
            logger.info("Saved execution record: %s", execution.execution_id)
        except Exception as e:
            logger.error("Failed to save execution record: %s", e)

    async def fire_event(
        self, trigger_type: TriggerType | str, event_context: dict[str, Any]
    ) -> int:
        """Fire an event that may trigger workflows.

        Args:
            trigger_type: TriggerType enum or string value (e.g. "file_write").
            event_context: Event data.

        Returns:
            Number of workflows triggered.
        """
        key = trigger_type if isinstance(trigger_type, str) else trigger_type.value
        event_context["trigger_type"] = key
        return await self.trigger_manager.fire_event(key, event_context)
