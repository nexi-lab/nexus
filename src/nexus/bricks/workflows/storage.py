"""Workflow storage layer — async, zero model imports.

Model classes (WorkflowModel, WorkflowExecutionModel) are injected via
the constructor so this module has no imports from nexus.storage.
All methods are async using SQLAlchemy AsyncSession.
"""

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import yaml
from sqlalchemy import select

from nexus.bricks.workflows.loader import WorkflowLoader
from nexus.bricks.workflows.types import WorkflowDefinition, WorkflowExecution
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


class WorkflowStore:
    """Async storage layer for workflow persistence."""

    def __init__(
        self,
        record_store: RecordStoreABC,
        *,
        workflow_model: type[Any],
        execution_model: type[Any],
        zone_id: str | None = None,
    ) -> None:
        """Initialize workflow store.

        Args:
            record_store: RecordStoreABC for database access (uses async_session_factory).
            workflow_model: SQLAlchemy model class for workflows.
            execution_model: SQLAlchemy model class for workflow executions.
            zone_id: Zone ID (defaults to ROOT_ZONE_ID).
        """
        self.session_factory = record_store.async_session_factory
        self._workflow_model = workflow_model
        self._execution_model = execution_model
        self.zone_id = zone_id or ROOT_ZONE_ID

    def _get_zone_id(self) -> str:
        return self.zone_id

    def _compute_hash(self, definition_yaml: str) -> str:
        return hashlib.sha256(definition_yaml.encode()).hexdigest()

    async def _get_workflow(
        self,
        session: Any,
        *,
        workflow_id: str | None = None,
        name: str | None = None,
    ) -> Any | None:
        """Fetch a single workflow by ID or name (DRY helper)."""
        if workflow_id is not None:
            stmt = select(self._workflow_model).where(
                self._workflow_model.workflow_id == workflow_id
            )
        elif name is not None:
            stmt = select(self._workflow_model).where(
                self._workflow_model.zone_id == self._get_zone_id(),
                self._workflow_model.name == name,
            )
        else:
            return None
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def save_workflow(self, definition: WorkflowDefinition, enabled: bool = True) -> str:
        """Save a workflow definition to database."""
        async with self.session_factory() as session:
            definition_dict: dict[str, Any] = {
                "name": definition.name,
                "version": definition.version,
                "description": definition.description,
            }

            if definition.variables:
                definition_dict["variables"] = definition.variables

            if definition.triggers:
                definition_dict["triggers"] = []
                for trigger in definition.triggers:
                    trigger_dict = {"type": trigger.type.value, **trigger.config}
                    definition_dict["triggers"].append(trigger_dict)

            definition_dict["actions"] = []
            for action in definition.actions:
                action_dict = {"name": action.name, "type": action.type, **action.config}
                definition_dict["actions"].append(action_dict)

            definition_yaml = yaml.dump(definition_dict, default_flow_style=False)
            definition_hash = self._compute_hash(definition_yaml)

            existing = await self._get_workflow(session, name=definition.name)

            if existing:
                existing.version = definition.version
                existing.description = definition.description
                existing.definition = definition_yaml
                existing.definition_hash = definition_hash
                existing.enabled = enabled
                existing.updated_at = datetime.now(UTC)
                workflow_id = str(existing.workflow_id)
                logger.info("Updated workflow: %s (id=%s)", definition.name, workflow_id)
            else:
                workflow = self._workflow_model(
                    workflow_id=str(uuid.uuid4()),
                    zone_id=self._get_zone_id(),
                    name=definition.name,
                    version=definition.version,
                    description=definition.description,
                    definition=definition_yaml,
                    definition_hash=definition_hash,
                    enabled=1 if enabled else 0,
                )
                session.add(workflow)
                workflow_id = str(workflow.workflow_id)
                logger.info("Created workflow: %s (id=%s)", definition.name, workflow_id)

            await session.commit()
            return workflow_id

    async def load_workflow(
        self,
        *,
        workflow_id: str | None = None,
        name: str | None = None,
    ) -> WorkflowDefinition | None:
        """Load a workflow definition by ID or name.

        Exactly one of ``workflow_id`` or ``name`` must be provided.
        """
        if workflow_id is None and name is None:
            raise ValueError("Exactly one of workflow_id or name is required")
        async with self.session_factory() as session:
            workflow = await self._get_workflow(session, workflow_id=workflow_id, name=name)
            if not workflow:
                return None
            try:
                return WorkflowLoader.load_from_string(workflow.definition)
            except Exception as e:
                key = workflow_id or name
                logger.error("Failed to parse workflow %s: %s", key, e)
                return None

    async def list_workflows(self) -> list[dict[str, Any]]:
        """List all workflows."""
        async with self.session_factory() as session:
            stmt = select(self._workflow_model).where(
                self._workflow_model.zone_id == self._get_zone_id()
            )
            result = await session.execute(stmt)
            workflows = result.scalars().all()

            items = []
            for workflow in workflows:
                try:
                    definition = WorkflowLoader.load_from_string(workflow.definition)
                    items.append(
                        {
                            "workflow_id": workflow.workflow_id,
                            "name": workflow.name,
                            "version": workflow.version,
                            "description": workflow.description,
                            "enabled": bool(workflow.enabled),
                            "triggers": len(definition.triggers),
                            "actions": len(definition.actions),
                            "created_at": workflow.created_at,
                            "updated_at": workflow.updated_at,
                        }
                    )
                except Exception as e:
                    logger.error("Failed to parse workflow %s: %s", workflow.workflow_id, e)

            return items

    async def delete_workflow(
        self,
        *,
        workflow_id: str | None = None,
        name: str | None = None,
    ) -> bool:
        """Delete a workflow by ID or name.

        Exactly one of ``workflow_id`` or ``name`` must be provided.
        """
        if workflow_id is None and name is None:
            raise ValueError("Exactly one of workflow_id or name is required")
        async with self.session_factory() as session:
            workflow = await self._get_workflow(session, workflow_id=workflow_id, name=name)
            if not workflow:
                return False
            await session.delete(workflow)
            await session.commit()
            logger.info("Deleted workflow: %s (id=%s)", workflow.name, workflow.workflow_id)
            return True

    async def set_enabled(
        self,
        enabled: bool,
        *,
        workflow_id: str | None = None,
        name: str | None = None,
    ) -> bool:
        """Enable or disable a workflow by ID or name.

        Exactly one of ``workflow_id`` or ``name`` must be provided.
        """
        if workflow_id is None and name is None:
            raise ValueError("Exactly one of workflow_id or name is required")
        async with self.session_factory() as session:
            workflow = await self._get_workflow(session, workflow_id=workflow_id, name=name)
            if not workflow:
                return False
            workflow.enabled = 1 if enabled else 0
            workflow.updated_at = datetime.now(UTC)
            await session.commit()
            logger.info("Set workflow %s enabled=%s", workflow.name, enabled)
            return True

    async def save_execution(self, execution: WorkflowExecution) -> str:
        """Save workflow execution record."""
        async with self.session_factory() as session:
            action_results_json = json.dumps(
                [
                    {
                        "action_name": r.action_name,
                        "success": r.success,
                        "output": r.output,
                        "error": r.error,
                        "duration_ms": r.duration_ms,
                    }
                    for r in execution.action_results
                ]
            )

            context_dict = {**execution.context, "action_results": action_results_json}

            execution_model = self._execution_model(
                execution_id=str(execution.execution_id),
                workflow_id=str(execution.workflow_id),
                trigger_type=execution.trigger_type.value,
                trigger_context=json.dumps(execution.trigger_context),
                status=execution.status.value,
                started_at=execution.started_at,
                completed_at=execution.completed_at,
                actions_completed=execution.actions_completed,
                actions_total=execution.actions_total,
                error_message=execution.error_message,
                context=json.dumps(context_dict),
            )

            session.add(execution_model)
            await session.commit()
            logger.info(
                "Saved execution: %s (status=%s)", execution.execution_id, execution.status.value
            )
            return str(execution.execution_id)

    async def get_executions(
        self,
        *,
        workflow_id: str | None = None,
        name: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Get execution history for a workflow.

        Accepts ``workflow_id`` or ``name`` (exactly one required).
        When ``name`` is given, resolves to workflow_id in the same session
        (single-session instead of double-session).
        """
        if workflow_id is None and name is None:
            raise ValueError("Exactly one of workflow_id or name is required")
        async with self.session_factory() as session:
            # Resolve name → workflow_id in same session (avoids double-session)
            if workflow_id is None:
                workflow = await self._get_workflow(session, name=name)
                if not workflow:
                    return []
                workflow_id = str(workflow.workflow_id)

            stmt = (
                select(self._execution_model)
                .where(self._execution_model.workflow_id == workflow_id)
                .order_by(self._execution_model.started_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            executions = result.scalars().all()

            items = []
            for execution in executions:
                items.append(
                    {
                        "execution_id": execution.execution_id,
                        "workflow_id": execution.workflow_id,
                        "trigger_type": execution.trigger_type,
                        "status": execution.status,
                        "started_at": execution.started_at,
                        "completed_at": execution.completed_at,
                        "actions_completed": execution.actions_completed,
                        "actions_total": execution.actions_total,
                        "error_message": execution.error_message,
                    }
                )

            return items
