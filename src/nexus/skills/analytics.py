"""Skill analytics and usage tracking."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from nexus.core.exceptions import ValidationError

logger = logging.getLogger(__name__)


class DatabaseConnection(Protocol):
    """Protocol for database connections.

    This allows analytics to work with different database backends.
    """

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


@dataclass
class SkillUsageRecord:
    """Record of skill usage for analytics."""

    usage_id: str
    skill_name: str
    agent_id: str | None
    tenant_id: str | None
    execution_time: float | None
    success: bool
    error_message: str | None
    timestamp: datetime

    def validate(self) -> None:
        """Validate usage record.

        Raises:
            ValidationError: If validation fails.
        """
        if not self.usage_id:
            raise ValidationError("usage_id is required")

        if not self.skill_name:
            raise ValidationError("skill_name is required")

        if self.execution_time is not None and self.execution_time < 0:
            raise ValidationError(f"execution_time cannot be negative, got {self.execution_time}")


@dataclass
class SkillAnalytics:
    """Analytics data for a skill."""

    skill_name: str
    usage_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    success_rate: float = 0.0
    avg_execution_time: float | None = None
    total_execution_time: float = 0.0
    unique_users: int = 0
    last_used: datetime | None = None
    first_used: datetime | None = None

    def calculate_success_rate(self) -> None:
        """Calculate success rate from counts."""
        if self.usage_count > 0:
            self.success_rate = self.success_count / self.usage_count
        else:
            self.success_rate = 0.0


@dataclass
class DashboardMetrics:
    """Dashboard metrics for organization-wide analytics."""

    total_skills: int = 0
    total_usage_count: int = 0
    total_users: int = 0
    most_used_skills: list[tuple[str, int]] = field(default_factory=list)
    top_contributors: list[tuple[str, int]] = field(default_factory=list)
    success_rates: dict[str, float] = field(default_factory=dict)
    avg_execution_times: dict[str, float] = field(default_factory=dict)


class SkillAnalyticsTracker:
    """Tracker for skill usage analytics.

    Features:
    - Track skill usage with execution time, success/failure
    - Calculate success rates and performance metrics
    - Track unique users and fork counts
    - Generate dashboard metrics for org-wide analytics

    Example:
        >>> from nexus.skills import SkillAnalyticsTracker
        >>>
        >>> # Initialize tracker with database connection
        >>> tracker = SkillAnalyticsTracker(db_connection)
        >>>
        >>> # Track skill usage
        >>> await tracker.track_usage(
        ...     "analyze-code",
        ...     agent_id="alice",
        ...     execution_time=1.5,
        ...     success=True
        ... )
        >>>
        >>> # Get analytics for a skill
        >>> analytics = await tracker.get_skill_analytics("analyze-code")
        >>> print(f"Success rate: {analytics.success_rate:.2%}")
        >>>
        >>> # Get dashboard metrics
        >>> metrics = await tracker.get_dashboard_metrics()
        >>> print(f"Total skills: {metrics.total_skills}")
    """

    def __init__(self, db_connection: DatabaseConnection | None = None):
        """Initialize analytics tracker.

        Args:
            db_connection: Optional database connection (defaults to in-memory)
        """
        self._db = db_connection
        self._in_memory_records: list[SkillUsageRecord] = []

    async def track_usage(
        self,
        skill_name: str,
        agent_id: str | None = None,
        tenant_id: str | None = None,
        execution_time: float | None = None,
        success: bool = True,
        error_message: str | None = None,
    ) -> str:
        """Track skill usage.

        Args:
            skill_name: Name of the skill
            agent_id: Optional agent ID
            tenant_id: Optional tenant ID
            execution_time: Execution time in seconds
            success: Whether the skill execution succeeded
            error_message: Optional error message if failed

        Returns:
            Usage ID

        Example:
            >>> usage_id = await tracker.track_usage(
            ...     "analyze-code",
            ...     agent_id="alice",
            ...     execution_time=1.5,
            ...     success=True
            ... )
        """
        usage_id = str(uuid.uuid4())
        timestamp = datetime.now(UTC)

        record = SkillUsageRecord(
            usage_id=usage_id,
            skill_name=skill_name,
            agent_id=agent_id,
            tenant_id=tenant_id,
            execution_time=execution_time,
            success=success,
            error_message=error_message,
            timestamp=timestamp,
        )

        record.validate()

        if self._db:
            # Insert into database
            query = """
            INSERT INTO skill_usage (
                usage_id, skill_name, agent_id, tenant_id,
                execution_time, success, error_message, timestamp
            ) VALUES (
                :usage_id, :skill_name, :agent_id, :tenant_id,
                :execution_time, :success, :error_message, :timestamp
            )
            """
            self._db.execute(
                query,
                {
                    "usage_id": usage_id,
                    "skill_name": skill_name,
                    "agent_id": agent_id,
                    "tenant_id": tenant_id,
                    "execution_time": execution_time,
                    "success": success,
                    "error_message": error_message,
                    "timestamp": timestamp,
                },
            )
            self._db.commit()
        else:
            # Store in memory
            self._in_memory_records.append(record)

        logger.debug(f"Tracked usage for skill '{skill_name}': success={success}")
        return usage_id

    async def get_skill_analytics(
        self, skill_name: str, tenant_id: str | None = None
    ) -> SkillAnalytics:
        """Get analytics for a skill.

        Args:
            skill_name: Name of the skill
            tenant_id: Optional tenant ID to filter by

        Returns:
            SkillAnalytics object with aggregated metrics

        Example:
            >>> analytics = await tracker.get_skill_analytics("analyze-code")
            >>> print(f"Usage: {analytics.usage_count}")
            >>> print(f"Success rate: {analytics.success_rate:.2%}")
            >>> print(f"Avg time: {analytics.avg_execution_time:.2f}s")
        """
        if self._db:
            # Query database
            query = """
            SELECT
                COUNT(*) as usage_count,
                SUM(CASE WHEN success THEN 1 ELSE 0 END) as success_count,
                SUM(CASE WHEN NOT success THEN 1 ELSE 0 END) as failure_count,
                AVG(CASE WHEN execution_time IS NOT NULL THEN execution_time END) as avg_execution_time,
                SUM(COALESCE(execution_time, 0)) as total_execution_time,
                COUNT(DISTINCT agent_id) as unique_users,
                MAX(timestamp) as last_used,
                MIN(timestamp) as first_used
            FROM skill_usage
            WHERE skill_name = :skill_name
            """
            params = {"skill_name": skill_name}

            if tenant_id:
                query += " AND tenant_id = :tenant_id"
                params["tenant_id"] = tenant_id

            result = self._db.fetchone(query, params)

            if not result:
                return SkillAnalytics(skill_name=skill_name)

            analytics = SkillAnalytics(
                skill_name=skill_name,
                usage_count=result.get("usage_count", 0),
                success_count=result.get("success_count", 0),
                failure_count=result.get("failure_count", 0),
                avg_execution_time=result.get("avg_execution_time"),
                total_execution_time=result.get("total_execution_time", 0.0),
                unique_users=result.get("unique_users", 0),
                last_used=result.get("last_used"),
                first_used=result.get("first_used"),
            )
            analytics.calculate_success_rate()
            return analytics

        else:
            # Calculate from in-memory records
            records = [r for r in self._in_memory_records if r.skill_name == skill_name]

            if tenant_id:
                records = [r for r in records if r.tenant_id == tenant_id]

            if not records:
                return SkillAnalytics(skill_name=skill_name)

            usage_count = len(records)
            success_count = sum(1 for r in records if r.success)
            failure_count = usage_count - success_count

            execution_times = [r.execution_time for r in records if r.execution_time is not None]
            avg_execution_time = (
                sum(execution_times) / len(execution_times) if execution_times else None
            )
            total_execution_time = sum(execution_times) if execution_times else 0.0

            unique_users = len({r.agent_id for r in records if r.agent_id})
            last_used = max(r.timestamp for r in records) if records else None
            first_used = min(r.timestamp for r in records) if records else None

            analytics = SkillAnalytics(
                skill_name=skill_name,
                usage_count=usage_count,
                success_count=success_count,
                failure_count=failure_count,
                avg_execution_time=avg_execution_time,
                total_execution_time=total_execution_time,
                unique_users=unique_users,
                last_used=last_used,
                first_used=first_used,
            )
            analytics.calculate_success_rate()
            return analytics

    async def get_dashboard_metrics(self, tenant_id: str | None = None) -> DashboardMetrics:
        """Get organization-wide dashboard metrics.

        Args:
            tenant_id: Optional tenant ID to filter by

        Returns:
            DashboardMetrics with aggregated org-wide metrics

        Example:
            >>> metrics = await tracker.get_dashboard_metrics()
            >>> print(f"Total skills: {metrics.total_skills}")
            >>> print(f"Most used: {metrics.most_used_skills[:5]}")
            >>> print(f"Top contributors: {metrics.top_contributors[:5]}")
        """
        if self._db:
            # Query database for dashboard metrics
            # Most used skills
            query = """
            SELECT skill_name, COUNT(*) as usage_count
            FROM skill_usage
            """
            params: dict[str, Any] = {}

            if tenant_id:
                query += " WHERE tenant_id = :tenant_id"
                params["tenant_id"] = tenant_id

            query += " GROUP BY skill_name ORDER BY usage_count DESC LIMIT 10"
            most_used = self._db.fetchall(query, params)
            most_used_skills = [(row["skill_name"], row["usage_count"]) for row in most_used]

            # Top contributors
            query = """
            SELECT agent_id, COUNT(*) as contribution_count
            FROM skill_usage
            WHERE agent_id IS NOT NULL
            """

            if tenant_id:
                query += " AND tenant_id = :tenant_id"

            query += " GROUP BY agent_id ORDER BY contribution_count DESC LIMIT 10"
            top_contrib = self._db.fetchall(query, params if tenant_id else None)
            top_contributors = [(row["agent_id"], row["contribution_count"]) for row in top_contrib]

            # Overall stats
            query = """
            SELECT
                COUNT(DISTINCT skill_name) as total_skills,
                COUNT(*) as total_usage,
                COUNT(DISTINCT agent_id) as total_users
            FROM skill_usage
            """

            if tenant_id:
                query += " WHERE tenant_id = :tenant_id"

            stats = self._db.fetchone(query, params if tenant_id else None)

            # Success rates per skill
            query = """
            SELECT
                skill_name,
                CAST(SUM(CASE WHEN success THEN 1 ELSE 0 END) AS FLOAT) / COUNT(*) as success_rate
            FROM skill_usage
            """

            if tenant_id:
                query += " WHERE tenant_id = :tenant_id"

            query += " GROUP BY skill_name"
            success_rates_rows = self._db.fetchall(query, params if tenant_id else None)
            success_rates = {row["skill_name"]: row["success_rate"] for row in success_rates_rows}

            # Avg execution times
            query = """
            SELECT
                skill_name,
                AVG(execution_time) as avg_time
            FROM skill_usage
            WHERE execution_time IS NOT NULL
            """

            if tenant_id:
                query += " AND tenant_id = :tenant_id"

            query += " GROUP BY skill_name"
            avg_times_rows = self._db.fetchall(query, params if tenant_id else None)
            avg_execution_times = {
                row["skill_name"]: row["avg_time"]
                for row in avg_times_rows
                if row["avg_time"] is not None
            }

            return DashboardMetrics(
                total_skills=stats.get("total_skills", 0) if stats else 0,
                total_usage_count=stats.get("total_usage", 0) if stats else 0,
                total_users=stats.get("total_users", 0) if stats else 0,
                most_used_skills=most_used_skills,
                top_contributors=top_contributors,
                success_rates=success_rates,
                avg_execution_times=avg_execution_times,
            )

        else:
            # Calculate from in-memory records
            records = self._in_memory_records

            if tenant_id:
                records = [r for r in records if r.tenant_id == tenant_id]

            # Count usage by skill
            usage_by_skill: dict[str, int] = {}
            for record in records:
                usage_by_skill[record.skill_name] = usage_by_skill.get(record.skill_name, 0) + 1

            most_used_skills = sorted(usage_by_skill.items(), key=lambda x: x[1], reverse=True)[:10]

            # Count contributions by agent
            contributions: dict[str, int] = {}
            for record in records:
                if record.agent_id:
                    contributions[record.agent_id] = contributions.get(record.agent_id, 0) + 1

            top_contributors = sorted(contributions.items(), key=lambda x: x[1], reverse=True)[:10]

            # Success rates
            success_rates = {}
            for skill_name in usage_by_skill:
                skill_records = [r for r in records if r.skill_name == skill_name]
                successes = sum(1 for r in skill_records if r.success)
                success_rates[skill_name] = successes / len(skill_records) if skill_records else 0.0

            # Avg execution times
            avg_execution_times = {}
            for skill_name in usage_by_skill:
                skill_records = [r for r in records if r.skill_name == skill_name]
                times = [r.execution_time for r in skill_records if r.execution_time is not None]
                if times:
                    avg_execution_times[skill_name] = sum(times) / len(times)

            return DashboardMetrics(
                total_skills=len(usage_by_skill),
                total_usage_count=len(records),
                total_users=len(contributions),
                most_used_skills=most_used_skills,
                top_contributors=top_contributors,
                success_rates=success_rates,
                avg_execution_times=avg_execution_times,
            )
