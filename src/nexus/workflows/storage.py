"""Workflow storage layer for database persistence."""

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import yaml
from sqlalchemy import select

from nexus.storage.models import WorkflowExecutionModel, WorkflowModel
from nexus.workflows.loader import WorkflowLoader
from nexus.workflows.types import WorkflowDefinition, WorkflowExecution

logger = logging.getLogger(__name__)


class WorkflowStore:
    """Storage layer for workflow persistence."""

    def __init__(self, session_factory, tenant_id: str | None = None):  # type: ignore[no-untyped-def]
        """Initialize workflow store.

        Args:
            session_factory: SQLAlchemy session factory
            tenant_id: Tenant ID (optional, defaults to "default")
        """
        self.session_factory = session_factory
        self.tenant_id = tenant_id or "default"

    def _get_tenant_id(self) -> str:
        """Get current tenant ID."""
        return self.tenant_id

    def _compute_hash(self, definition_yaml: str) -> str:
        """Compute SHA256 hash of workflow definition.

        Args:
            definition_yaml: YAML workflow definition

        Returns:
            SHA256 hash hex string
        """
        return hashlib.sha256(definition_yaml.encode()).hexdigest()

    def save_workflow(self, definition: WorkflowDefinition, enabled: bool = True) -> str:
        """Save a workflow definition to database.

        Args:
            definition: Workflow definition
            enabled: Whether workflow is enabled

        Returns:
            Workflow ID (UUID string)
        """
        with self.session_factory() as session:
            # Convert definition to YAML
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

            # Check if workflow already exists
            stmt = select(WorkflowModel).where(
                WorkflowModel.tenant_id == self._get_tenant_id(),
                WorkflowModel.name == definition.name,
            )
            existing = session.execute(stmt).scalar_one_or_none()

            if existing:
                # Update existing workflow
                existing.version = definition.version
                existing.description = definition.description
                existing.definition = definition_yaml
                existing.definition_hash = definition_hash
                existing.enabled = enabled
                existing.updated_at = datetime.now(UTC)
                workflow_id = str(existing.workflow_id)
                logger.info(f"Updated workflow: {definition.name} (id={workflow_id})")
            else:
                # Create new workflow
                workflow = WorkflowModel(
                    workflow_id=str(uuid.uuid4()),
                    tenant_id=self._get_tenant_id(),
                    name=definition.name,
                    version=definition.version,
                    description=definition.description,
                    definition=definition_yaml,
                    definition_hash=definition_hash,
                    enabled=1 if enabled else 0,
                )
                session.add(workflow)
                workflow_id = str(workflow.workflow_id)
                logger.info(f"Created workflow: {definition.name} (id={workflow_id})")

            session.commit()
            return workflow_id

    def load_workflow(self, workflow_id: str) -> WorkflowDefinition | None:
        """Load a workflow definition from database.

        Args:
            workflow_id: Workflow UUID

        Returns:
            WorkflowDefinition or None if not found
        """
        with self.session_factory() as session:
            stmt = select(WorkflowModel).where(WorkflowModel.workflow_id == workflow_id)
            workflow = session.execute(stmt).scalar_one_or_none()

            if not workflow:
                return None

            # Parse YAML definition
            try:
                return WorkflowLoader.load_from_string(workflow.definition)
            except Exception as e:
                logger.error(f"Failed to parse workflow {workflow_id}: {e}")
                return None

    def load_workflow_by_name(self, name: str) -> WorkflowDefinition | None:
        """Load a workflow by name.

        Args:
            name: Workflow name

        Returns:
            WorkflowDefinition or None if not found
        """
        with self.session_factory() as session:
            stmt = select(WorkflowModel).where(
                WorkflowModel.tenant_id == self._get_tenant_id(),
                WorkflowModel.name == name,
            )
            workflow = session.execute(stmt).scalar_one_or_none()

            if not workflow:
                return None

            try:
                return WorkflowLoader.load_from_string(workflow.definition)
            except Exception as e:
                logger.error(f"Failed to parse workflow {name}: {e}")
                return None

    def list_workflows(self) -> list[dict[str, Any]]:
        """List all workflows.

        Returns:
            List of workflow info dictionaries
        """
        with self.session_factory() as session:
            stmt = select(WorkflowModel).where(WorkflowModel.tenant_id == self._get_tenant_id())
            workflows = session.execute(stmt).scalars().all()

            result = []
            for workflow in workflows:
                try:
                    definition = WorkflowLoader.load_from_string(workflow.definition)
                    result.append(
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
                    logger.error(f"Failed to parse workflow {workflow.workflow_id}: {e}")

            return result

    def delete_workflow(self, workflow_id: str) -> bool:
        """Delete a workflow.

        Args:
            workflow_id: Workflow UUID

        Returns:
            True if deleted, False if not found
        """
        with self.session_factory() as session:
            stmt = select(WorkflowModel).where(WorkflowModel.workflow_id == workflow_id)
            workflow = session.execute(stmt).scalar_one_or_none()

            if not workflow:
                return False

            session.delete(workflow)
            session.commit()
            logger.info(f"Deleted workflow: {workflow.name} (id={workflow_id})")
            return True

    def delete_workflow_by_name(self, name: str) -> bool:
        """Delete a workflow by name.

        Args:
            name: Workflow name

        Returns:
            True if deleted, False if not found
        """
        with self.session_factory() as session:
            stmt = select(WorkflowModel).where(
                WorkflowModel.tenant_id == self._get_tenant_id(),
                WorkflowModel.name == name,
            )
            workflow = session.execute(stmt).scalar_one_or_none()

            if not workflow:
                return False

            session.delete(workflow)
            session.commit()
            logger.info(f"Deleted workflow: {name}")
            return True

    def set_enabled(self, workflow_id: str, enabled: bool) -> bool:
        """Enable or disable a workflow.

        Args:
            workflow_id: Workflow UUID
            enabled: True to enable, False to disable

        Returns:
            True if updated, False if not found
        """
        with self.session_factory() as session:
            stmt = select(WorkflowModel).where(WorkflowModel.workflow_id == workflow_id)
            workflow = session.execute(stmt).scalar_one_or_none()

            if not workflow:
                return False

            workflow.enabled = 1 if enabled else 0
            workflow.updated_at = datetime.now(UTC)
            session.commit()
            logger.info(f"Set workflow {workflow.name} enabled={enabled}")
            return True

    def set_enabled_by_name(self, name: str, enabled: bool) -> bool:
        """Enable or disable a workflow by name.

        Args:
            name: Workflow name
            enabled: True to enable, False to disable

        Returns:
            True if updated, False if not found
        """
        with self.session_factory() as session:
            stmt = select(WorkflowModel).where(
                WorkflowModel.tenant_id == self._get_tenant_id(),
                WorkflowModel.name == name,
            )
            workflow = session.execute(stmt).scalar_one_or_none()

            if not workflow:
                return False

            workflow.enabled = 1 if enabled else 0
            workflow.updated_at = datetime.now(UTC)
            session.commit()
            logger.info(f"Set workflow {name} enabled={enabled}")
            return True

    def save_execution(self, execution: WorkflowExecution) -> str:
        """Save workflow execution record.

        Args:
            execution: Workflow execution record

        Returns:
            Execution ID (UUID string)
        """
        with self.session_factory() as session:
            # Convert action results to JSON
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

            # Combine context with action results
            context_dict = {**execution.context, "action_results": action_results_json}

            execution_model = WorkflowExecutionModel(
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
            session.commit()
            logger.info(
                f"Saved execution: {execution.execution_id} (status={execution.status.value})"
            )
            return str(execution.execution_id)

    def get_executions(self, workflow_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """Get execution history for a workflow.

        Args:
            workflow_id: Workflow UUID
            limit: Maximum number of executions to return

        Returns:
            List of execution records
        """
        with self.session_factory() as session:
            stmt = (
                select(WorkflowExecutionModel)
                .where(WorkflowExecutionModel.workflow_id == workflow_id)
                .order_by(WorkflowExecutionModel.started_at.desc())
                .limit(limit)
            )
            executions = session.execute(stmt).scalars().all()

            result = []
            for execution in executions:
                result.append(
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

            return result

    def get_executions_by_name(self, name: str, limit: int = 10) -> list[dict[str, Any]]:
        """Get execution history for a workflow by name.

        Args:
            name: Workflow name
            limit: Maximum number of executions to return

        Returns:
            List of execution records
        """
        # First get workflow ID
        with self.session_factory() as session:
            stmt = select(WorkflowModel).where(
                WorkflowModel.tenant_id == self._get_tenant_id(),
                WorkflowModel.name == name,
            )
            workflow = session.execute(stmt).scalar_one_or_none()

            if not workflow:
                return []

            return self.get_executions(workflow.workflow_id, limit)
