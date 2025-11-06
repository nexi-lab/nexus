"""Skill audit logging and compliance tracking."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from nexus.core.exceptions import ValidationError

logger = logging.getLogger(__name__)


class DatabaseConnection(Protocol):
    """Protocol for database connections."""

    def execute(self, query: str, params: dict[str, Any] | None = None) -> Any:
        """Execute a query."""
        ...

    def fetchall(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Fetch all results from a query."""
        ...

    def fetchone(self, query: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Fetch one result from a query."""
        ...

    def commit(self) -> None:
        """Commit the transaction."""
        ...


class AuditAction(StrEnum):
    """Types of auditable actions for skills."""

    CREATED = "created"
    EXECUTED = "executed"
    FORKED = "forked"
    PUBLISHED = "published"
    DELETED = "deleted"
    UPDATED = "updated"


@dataclass
class AuditLogEntry:
    """Audit log entry for skill operations."""

    audit_id: str
    skill_name: str
    action: AuditAction
    agent_id: str | None
    tenant_id: str | None
    details: dict[str, Any] | None
    timestamp: datetime

    def validate(self) -> None:
        """Validate audit log entry.

        Raises:
            ValidationError: If validation fails.
        """
        if not self.audit_id:
            raise ValidationError("audit_id is required")

        if not self.skill_name:
            raise ValidationError("skill_name is required")

        if not isinstance(self.action, AuditAction):
            raise ValidationError(f"action must be AuditAction, got {type(self.action)}")


class SkillAuditLogger:
    """Audit logger for skill operations.

    Features:
    - Log all skill usage and modifications
    - Track execution details (inputs, outputs, errors)
    - Store findings and results for compliance
    - Query audit logs by skill, action, agent, or time range
    - Generate compliance reports

    Example:
        >>> from nexus.skills import SkillAuditLogger, AuditAction
        >>>
        >>> # Initialize logger
        >>> audit = SkillAuditLogger(db_connection)
        >>>
        >>> # Log skill execution
        >>> await audit.log(
        ...     "analyze-code",
        ...     AuditAction.EXECUTED,
        ...     agent_id="alice",
        ...     details={
        ...         "inputs": {"file": "main.py"},
        ...         "outputs": {"findings": ["unused import"]},
        ...         "execution_time": 1.5
        ...     }
        ... )
        >>>
        >>> # Query audit logs
        >>> logs = await audit.query_logs(skill_name="analyze-code")
        >>> for log in logs:
        ...     print(f"{log.action.value} by {log.agent_id} at {log.timestamp}")
    """

    def __init__(self, db_connection: DatabaseConnection | None = None):
        """Initialize audit logger.

        Args:
            db_connection: Optional database connection (defaults to in-memory)
        """
        self._db = db_connection
        self._in_memory_logs: list[AuditLogEntry] = []

    async def log(
        self,
        skill_name: str,
        action: AuditAction,
        agent_id: str | None = None,
        tenant_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> str:
        """Log a skill operation for audit trail.

        Args:
            skill_name: Name of the skill
            action: Type of action performed
            agent_id: Optional agent ID
            tenant_id: Optional tenant ID
            details: Optional additional context (inputs, outputs, findings, etc.)

        Returns:
            Audit log entry ID

        Example:
            >>> audit_id = await audit.log(
            ...     "data-processor",
            ...     AuditAction.EXECUTED,
            ...     agent_id="alice",
            ...     details={
            ...         "inputs": {"dataset": "sales_2024.csv"},
            ...         "outputs": {"rows_processed": 10000},
            ...         "findings": ["duplicate entries found"],
            ...         "execution_time": 2.3
            ...     }
            ... )
        """
        audit_id = str(uuid.uuid4())
        timestamp = datetime.now(UTC)

        entry = AuditLogEntry(
            audit_id=audit_id,
            skill_name=skill_name,
            action=action,
            agent_id=agent_id,
            tenant_id=tenant_id,
            details=details,
            timestamp=timestamp,
        )

        entry.validate()

        if self._db:
            # Insert into database
            query = """
            INSERT INTO skill_audit_log (
                audit_id, skill_name, action, agent_id,
                tenant_id, details, timestamp
            ) VALUES (
                :audit_id, :skill_name, :action, :agent_id,
                :tenant_id, :details, :timestamp
            )
            """
            import json

            self._db.execute(
                query,
                {
                    "audit_id": audit_id,
                    "skill_name": skill_name,
                    "action": action.value,
                    "agent_id": agent_id,
                    "tenant_id": tenant_id,
                    "details": json.dumps(details) if details else None,
                    "timestamp": timestamp,
                },
            )
            self._db.commit()
        else:
            # Store in memory
            self._in_memory_logs.append(entry)

        logger.debug(f"Logged {action.value} for skill '{skill_name}' (ID: {audit_id})")
        return audit_id

    async def query_logs(
        self,
        skill_name: str | None = None,
        action: AuditAction | None = None,
        agent_id: str | None = None,
        tenant_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int | None = 100,
    ) -> list[AuditLogEntry]:
        """Query audit logs with filters.

        Args:
            skill_name: Optional skill name filter
            action: Optional action type filter
            agent_id: Optional agent ID filter
            tenant_id: Optional tenant ID filter
            start_time: Optional start time filter
            end_time: Optional end time filter
            limit: Maximum number of results (default: 100)

        Returns:
            List of matching audit log entries

        Example:
            >>> # Get all executions of a skill
            >>> logs = await audit.query_logs(
            ...     skill_name="analyze-code",
            ...     action=AuditAction.EXECUTED
            ... )
            >>>
            >>> # Get all actions by an agent
            >>> logs = await audit.query_logs(agent_id="alice")
            >>>
            >>> # Get recent activity
            >>> from datetime import datetime, timedelta, timezone
            >>> yesterday = datetime.now(timezone.utc) - timedelta(days=1)
            >>> logs = await audit.query_logs(start_time=yesterday)
        """
        if self._db:
            # Build query
            query = "SELECT * FROM skill_audit_log WHERE 1=1"
            params: dict[str, Any] = {}

            if skill_name:
                query += " AND skill_name = :skill_name"
                params["skill_name"] = skill_name

            if action:
                query += " AND action = :action"
                params["action"] = action.value

            if agent_id:
                query += " AND agent_id = :agent_id"
                params["agent_id"] = agent_id

            if tenant_id:
                query += " AND tenant_id = :tenant_id"
                params["tenant_id"] = tenant_id

            if start_time:
                query += " AND timestamp >= :start_time"
                params["start_time"] = start_time

            if end_time:
                query += " AND timestamp <= :end_time"
                params["end_time"] = end_time

            query += " ORDER BY timestamp DESC"

            if limit:
                query += f" LIMIT {limit}"

            import json

            results = self._db.fetchall(query, params)
            logs = []
            for row in results:
                details = json.loads(row["details"]) if row.get("details") else None
                logs.append(
                    AuditLogEntry(
                        audit_id=row["audit_id"],
                        skill_name=row["skill_name"],
                        action=AuditAction(row["action"]),
                        agent_id=row.get("agent_id"),
                        tenant_id=row.get("tenant_id"),
                        details=details,
                        timestamp=row["timestamp"],
                    )
                )
            return logs

        else:
            # Filter in-memory logs
            logs = self._in_memory_logs

            if skill_name:
                logs = [log for log in logs if log.skill_name == skill_name]

            if action:
                logs = [log for log in logs if log.action == action]

            if agent_id:
                logs = [log for log in logs if log.agent_id == agent_id]

            if tenant_id:
                logs = [log for log in logs if log.tenant_id == tenant_id]

            if start_time:
                logs = [log for log in logs if log.timestamp >= start_time]

            if end_time:
                logs = [log for log in logs if log.timestamp <= end_time]

            # Sort by timestamp descending
            logs = sorted(logs, key=lambda x: x.timestamp, reverse=True)

            # Limit results
            if limit:
                logs = logs[:limit]

            return logs

    async def get_skill_activity(self, skill_name: str) -> dict[str, Any]:
        """Get activity summary for a skill.

        Args:
            skill_name: Name of the skill

        Returns:
            Dictionary with activity metrics

        Example:
            >>> activity = await audit.get_skill_activity("analyze-code")
            >>> print(f"Total executions: {activity['total_executions']}")
            >>> print(f"Unique users: {activity['unique_users']}")
            >>> print(f"Last activity: {activity['last_activity']}")
        """
        logs = await self.query_logs(skill_name=skill_name, limit=None)

        total_executions = sum(1 for log in logs if log.action == AuditAction.EXECUTED)
        unique_users = len({log.agent_id for log in logs if log.agent_id})
        last_activity = max(log.timestamp for log in logs) if logs else None

        # Count actions by type
        action_counts: dict[str, int] = {}
        for log in logs:
            action_counts[log.action.value] = action_counts.get(log.action.value, 0) + 1

        return {
            "skill_name": skill_name,
            "total_logs": len(logs),
            "total_executions": total_executions,
            "unique_users": unique_users,
            "last_activity": last_activity,
            "action_counts": action_counts,
        }

    async def generate_compliance_report(
        self,
        tenant_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> dict[str, Any]:
        """Generate a compliance report for audit purposes.

        Args:
            tenant_id: Optional tenant ID to filter by
            start_time: Optional start time
            end_time: Optional end time

        Returns:
            Dictionary with compliance metrics

        Example:
            >>> from datetime import datetime, timedelta, timezone
            >>> start = datetime.now(timezone.utc) - timedelta(days=30)
            >>> report = await audit.generate_compliance_report(start_time=start)
            >>> print(f"Total operations: {report['total_operations']}")
            >>> print(f"Skills used: {report['skills_used']}")
            >>> print(f"Active agents: {report['active_agents']}")
        """
        logs = await self.query_logs(
            tenant_id=tenant_id, start_time=start_time, end_time=end_time, limit=None
        )

        # Aggregate metrics
        skills_used = {log.skill_name for log in logs}
        active_agents = {log.agent_id for log in logs if log.agent_id}

        # Count by action
        action_counts: dict[str, int] = {}
        for log in logs:
            action_counts[log.action.value] = action_counts.get(log.action.value, 0) + 1

        # Count by skill
        skill_counts: dict[str, int] = {}
        for log in logs:
            skill_counts[log.skill_name] = skill_counts.get(log.skill_name, 0) + 1

        # Top skills
        top_skills = sorted(skill_counts.items(), key=lambda x: x[1], reverse=True)[:10]

        # Recent activity
        recent_logs = sorted(logs, key=lambda x: x.timestamp, reverse=True)[:20]
        recent_activity = [
            {
                "skill_name": log.skill_name,
                "action": log.action.value,
                "agent_id": log.agent_id,
                "timestamp": log.timestamp.isoformat() if log.timestamp else None,
            }
            for log in recent_logs
        ]

        return {
            "report_period": {
                "start": start_time.isoformat() if start_time else None,
                "end": end_time.isoformat() if end_time else None,
            },
            "tenant_id": tenant_id,
            "total_operations": len(logs),
            "skills_used": len(skills_used),
            "active_agents": len(active_agents),
            "action_counts": action_counts,
            "top_skills": top_skills,
            "recent_activity": recent_activity,
        }
