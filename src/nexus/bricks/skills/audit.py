"""Skill audit logging and compliance tracking."""

from __future__ import annotations

import dataclasses
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from nexus.bricks.skills.exceptions import SkillValidationError

if TYPE_CHECKING:
    from nexus.core.cache_store import CacheStoreABC

logger = logging.getLogger(__name__)


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
    zone_id: str | None
    details: dict[str, Any] | None
    timestamp: datetime

    def validate(self) -> None:
        """Validate audit log entry.

        Raises:
            ValidationError: If validation fails.
        """
        if not self.audit_id:
            raise SkillValidationError("audit_id is required")

        if not self.skill_name:
            raise SkillValidationError("skill_name is required")

        if not isinstance(self.action, AuditAction):
            raise SkillValidationError(f"action must be AuditAction, got {type(self.action)}")


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

_KEY_PREFIX = "skills:audit"


def _audit_key(audit_id: str) -> str:
    return f"{_KEY_PREFIX}:{audit_id}"


def _json_default(obj: object) -> str:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Not JSON serializable: {type(obj)}")


def _serialize_entry(entry: AuditLogEntry) -> bytes:
    return json.dumps(dataclasses.asdict(entry), default=_json_default).encode()


def _deserialize_entry(raw: bytes) -> AuditLogEntry:
    data: dict[str, Any] = json.loads(raw)
    data["action"] = AuditAction(data["action"])
    data["timestamp"] = datetime.fromisoformat(data["timestamp"])
    return AuditLogEntry(**data)


class SkillAuditLogger:
    """Audit logger for skill operations.

    Features:
    - Log all skill usage and modifications
    - Track execution details (inputs, outputs, errors)
    - Store findings and results for compliance
    - Query audit logs by skill, action, agent, or time range
    - Generate compliance reports

    Example:
        >>> from nexus.bricks.skills.audit import SkillAuditLogger, AuditAction
        >>>
        >>> # Initialize logger
        >>> audit = SkillAuditLogger()
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

    def __init__(self, cache_store: CacheStoreABC | None = None) -> None:
        """Initialize audit logger.

        Args:
            cache_store: Optional CacheStoreABC for ephemeral log storage.
                         Defaults to InMemoryCacheStore when *None*.
        """
        if cache_store is not None:
            self._cache: CacheStoreABC = cache_store
        else:
            from nexus.cache.inmemory import InMemoryCacheStore

            self._cache = InMemoryCacheStore()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _all_entries(self) -> list[AuditLogEntry]:
        """Load all audit entries from the cache."""
        keys = await self._cache.keys_by_pattern(f"{_KEY_PREFIX}:*")
        if not keys:
            return []
        values = await self._cache.get_many(keys)
        return [_deserialize_entry(v) for v in values.values() if v is not None]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def log(
        self,
        skill_name: str,
        action: AuditAction,
        agent_id: str | None = None,
        zone_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> str:
        """Log a skill operation for audit trail.

        Args:
            skill_name: Name of the skill
            action: Type of action performed
            agent_id: Optional agent ID
            zone_id: Optional zone ID
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
            zone_id=zone_id,
            details=details,
            timestamp=timestamp,
        )

        entry.validate()

        await self._cache.set(_audit_key(audit_id), _serialize_entry(entry))

        logger.debug(f"Logged {action.value} for skill '{skill_name}' (ID: {audit_id})")
        return audit_id

    async def query_logs(
        self,
        skill_name: str | None = None,
        action: AuditAction | None = None,
        agent_id: str | None = None,
        zone_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int | None = 100,
    ) -> list[AuditLogEntry]:
        """Query audit logs with filters.

        Args:
            skill_name: Optional skill name filter
            action: Optional action type filter
            agent_id: Optional agent ID filter
            zone_id: Optional zone ID filter
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
        logs = await self._all_entries()

        if skill_name:
            logs = [entry for entry in logs if entry.skill_name == skill_name]

        if action:
            logs = [entry for entry in logs if entry.action == action]

        if agent_id:
            logs = [entry for entry in logs if entry.agent_id == agent_id]

        if zone_id:
            logs = [entry for entry in logs if entry.zone_id == zone_id]

        if start_time:
            logs = [entry for entry in logs if entry.timestamp >= start_time]

        if end_time:
            logs = [entry for entry in logs if entry.timestamp <= end_time]

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

        total_executions = sum(1 for entry in logs if entry.action == AuditAction.EXECUTED)
        unique_users = len({entry.agent_id for entry in logs if entry.agent_id})
        last_activity = max(entry.timestamp for entry in logs) if logs else None

        # Count actions by type
        action_counts: dict[str, int] = {}
        for entry in logs:
            action_counts[entry.action.value] = action_counts.get(entry.action.value, 0) + 1

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
        zone_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> dict[str, Any]:
        """Generate a compliance report for audit purposes.

        Args:
            zone_id: Optional zone ID to filter by
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
            zone_id=zone_id, start_time=start_time, end_time=end_time, limit=None
        )

        # Aggregate metrics
        skills_used = {entry.skill_name for entry in logs}
        active_agents = {entry.agent_id for entry in logs if entry.agent_id}

        # Count by action
        action_counts: dict[str, int] = {}
        for entry in logs:
            action_counts[entry.action.value] = action_counts.get(entry.action.value, 0) + 1

        # Count by skill
        skill_counts: dict[str, int] = {}
        for entry in logs:
            skill_counts[entry.skill_name] = skill_counts.get(entry.skill_name, 0) + 1

        # Top skills
        top_skills = sorted(skill_counts.items(), key=lambda x: x[1], reverse=True)[:10]

        # Recent activity
        recent_logs = sorted(logs, key=lambda x: x.timestamp, reverse=True)[:20]
        recent_activity = [
            {
                "skill_name": entry.skill_name,
                "action": entry.action.value,
                "agent_id": entry.agent_id,
                "timestamp": entry.timestamp.isoformat() if entry.timestamp else None,
            }
            for entry in recent_logs
        ]

        return {
            "report_period": {
                "start": start_time.isoformat() if start_time else None,
                "end": end_time.isoformat() if end_time else None,
            },
            "zone_id": zone_id,
            "total_operations": len(logs),
            "skills_used": len(skills_used),
            "active_agents": len(active_agents),
            "action_counts": action_counts,
            "top_skills": top_skills,
            "recent_activity": recent_activity,
        }
