"""Skill user preferences management.

Controls which skills users grant access to for their agents.
Users can revoke specific skills from specific agents for safety or access control.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from nexus.core.exceptions import ValidationError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class DatabaseSession(Protocol):
    """Protocol for database session operations."""

    def execute(self, query: Any, params: dict[str, Any] | None = None) -> Any:
        """Execute a query."""
        ...

    def commit(self) -> None:
        """Commit the transaction."""
        ...

    def rollback(self) -> None:
        """Rollback the transaction."""
        ...


@dataclass
class SkillPreference:
    """User preference for granting/revoking skill access to an agent."""

    preference_id: str
    user_id: str
    agent_id: str  # Required: which agent to grant/revoke skill
    skill_name: str
    enabled: bool
    tenant_id: str | None = None
    reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def validate(self) -> None:
        """Validate preference data.

        Raises:
            ValidationError: If validation fails.
        """
        if not self.user_id:
            raise ValidationError("user_id is required")

        if not self.agent_id:
            raise ValidationError("agent_id is required")

        if not self.skill_name:
            raise ValidationError("skill_name is required")

        if not self.preference_id:
            raise ValidationError("preference_id is required")


class SkillPreferenceManager:
    """Manager for skill access control via user preferences.

    Controls which skills users grant to their agents. By default, all skills
    are granted (enabled) to all agents unless explicitly revoked.

    Features:
    - Grant/revoke skill access for specific agents
    - Safety controls (e.g., prevent SQL access for chatbots)
    - Bulk operations for efficiency
    - Integration with skill discovery/filtering

    Example:
        >>> from nexus import connect
        >>> from nexus.skills import SkillPreferenceManager
        >>>
        >>> nx = connect()
        >>> pref_mgr = SkillPreferenceManager(nx.db_session)
        >>>
        >>> # Revoke a dangerous skill from a chatbot agent
        >>> pref_mgr.set_preference(
        ...     user_id="alice",
        ...     agent_id="chatbot",
        ...     skill_name="sql-query",
        ...     enabled=False,
        ...     reason="Safety: prevent direct database access"
        ... )
        >>>
        >>> # Grant code-review skill to dev assistant
        >>> pref_mgr.set_preference(
        ...     user_id="alice",
        ...     agent_id="dev-assistant",
        ...     skill_name="code-review",
        ...     enabled=True
        ... )
        >>>
        >>> # Check if skill is granted
        >>> is_granted = pref_mgr.is_skill_enabled(
        ...     user_id="alice",
        ...     agent_id="chatbot",
        ...     skill_name="sql-query"
        ... )
        >>> print(is_granted)  # False (revoked for safety)
    """

    def __init__(self, db_session: Session):
        """Initialize preference manager.

        Args:
            db_session: SQLAlchemy session for database operations.
        """
        self._session = db_session

    def set_preference(
        self,
        user_id: str,
        agent_id: str,
        skill_name: str,
        enabled: bool,
        tenant_id: str | None = None,
        reason: str | None = None,
    ) -> SkillPreference:
        """Set skill access preference for a user's agent.

        Grants or revokes skill access for a specific agent. Creates or updates
        the preference record.

        Args:
            user_id: User identifier
            agent_id: Agent identifier (required)
            skill_name: Name of the skill
            enabled: True to grant access, False to revoke
            tenant_id: Optional tenant identifier for tenant isolation
            reason: Optional reason for the grant/revoke (audit trail)

        Returns:
            SkillPreference object with the saved preference

        Raises:
            ValidationError: If inputs are invalid
        """
        from nexus.storage.models import SkillUserPreferenceModel

        # Check if preference already exists
        existing = self._get_preference_record(user_id, agent_id, skill_name)

        if existing:
            # Update existing preference
            existing.enabled = enabled
            existing.reason = reason
            existing.updated_at = datetime.now(UTC)
            self._session.commit()

            return SkillPreference(
                preference_id=existing.preference_id,
                user_id=existing.user_id,
                agent_id=existing.agent_id,
                tenant_id=existing.tenant_id,
                skill_name=existing.skill_name,
                enabled=bool(existing.enabled),
                reason=existing.reason,
                created_at=existing.created_at,
                updated_at=existing.updated_at,
            )

        # Create new preference
        preference_id = str(uuid.uuid4())
        now = datetime.now(UTC)

        new_pref = SkillUserPreferenceModel(
            preference_id=preference_id,
            user_id=user_id,
            agent_id=agent_id,
            tenant_id=tenant_id,
            skill_name=skill_name,
            enabled=enabled,
            reason=reason,
            created_at=now,
            updated_at=now,
        )

        self._session.add(new_pref)
        self._session.commit()

        return SkillPreference(
            preference_id=preference_id,
            user_id=user_id,
            agent_id=agent_id,
            tenant_id=tenant_id,
            skill_name=skill_name,
            enabled=enabled,
            reason=reason,
            created_at=now,
            updated_at=now,
        )

    def get_preference(
        self, user_id: str, agent_id: str, skill_name: str
    ) -> SkillPreference | None:
        """Get preference for a specific user/agent/skill combination.

        Args:
            user_id: User identifier
            agent_id: Agent identifier (required)
            skill_name: Name of the skill

        Returns:
            SkillPreference if found, None otherwise
        """
        record = self._get_preference_record(user_id, agent_id, skill_name)

        if not record:
            return None

        return SkillPreference(
            preference_id=record.preference_id,
            user_id=record.user_id,
            agent_id=record.agent_id,
            tenant_id=record.tenant_id,
            skill_name=record.skill_name,
            enabled=bool(record.enabled),
            reason=record.reason,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    def is_skill_enabled(
        self,
        user_id: str,
        agent_id: str,
        skill_name: str,
        tenant_id: str | None = None,  # noqa: ARG002
    ) -> bool:
        """Check if a skill is granted to an agent.

        Default behavior: Skills are granted (True) unless explicitly revoked.

        Args:
            user_id: User identifier
            agent_id: Agent identifier (required)
            skill_name: Name of the skill
            tenant_id: Optional tenant identifier (for future tenant-level defaults)

        Returns:
            True if skill is granted/enabled, False if revoked/disabled
        """
        # Check for explicit preference
        pref = self.get_preference(user_id, agent_id, skill_name)
        if pref is not None:
            return pref.enabled

        # TODO: Check tenant-level default preferences (future enhancement)
        # if tenant_id:
        #     tenant_pref = self.get_tenant_default(tenant_id, skill_name)
        #     if tenant_pref is not None:
        #         return tenant_pref.enabled

        # Default: skills are granted (enabled)
        return True

    def list_user_preferences(
        self, user_id: str, agent_id: str | None = None, enabled_only: bool | None = None
    ) -> list[SkillPreference]:
        """List all preferences for a user/agent.

        Args:
            user_id: User identifier
            agent_id: Optional agent identifier (None = user-level only)
            enabled_only: If True, return only enabled skills; if False, only disabled; if None, all

        Returns:
            List of SkillPreference objects
        """
        from nexus.storage.models import SkillUserPreferenceModel

        query = self._session.query(SkillUserPreferenceModel).filter(
            SkillUserPreferenceModel.user_id == user_id
        )

        # Filter by agent_id
        if agent_id is not None:
            query = query.filter(SkillUserPreferenceModel.agent_id == agent_id)

        # Filter by enabled status
        if enabled_only is not None:
            query = query.filter(SkillUserPreferenceModel.enabled == enabled_only)

        records = query.all()

        return [
            SkillPreference(
                preference_id=record.preference_id,
                user_id=record.user_id,
                agent_id=record.agent_id,
                tenant_id=record.tenant_id,
                skill_name=record.skill_name,
                enabled=bool(record.enabled),
                reason=record.reason,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
            for record in records
        ]

    def filter_enabled_skills(
        self,
        user_id: str,
        agent_id: str,
        skill_names: list[str],
        tenant_id: str | None = None,
    ) -> list[str]:
        """Filter a list of skills to only those granted to the agent.

        This is the primary method for integrating with skill discovery/listing.

        Args:
            user_id: User identifier
            agent_id: Agent identifier (required)
            skill_names: List of skill names to filter
            tenant_id: Optional tenant identifier

        Returns:
            List of skill names that are granted/enabled (filtered list)

        Example:
            >>> all_skills = ["code-review", "sql-query", "refactor"]
            >>> enabled = pref_mgr.filter_enabled_skills("alice", "chatbot", all_skills)
            >>> print(enabled)  # ["code-review", "refactor"]  (sql-query was revoked)
        """
        return [
            skill_name
            for skill_name in skill_names
            if self.is_skill_enabled(user_id, agent_id, skill_name, tenant_id)
        ]

    def delete_preference(self, user_id: str, agent_id: str, skill_name: str) -> bool:
        """Delete a preference (reset to default grant).

        Args:
            user_id: User identifier
            agent_id: Agent identifier (required)
            skill_name: Name of the skill

        Returns:
            True if preference was deleted, False if it didn't exist
        """
        record = self._get_preference_record(user_id, agent_id, skill_name)

        if not record:
            return False

        self._session.delete(record)
        self._session.commit()
        return True

    def bulk_set_preferences(
        self,
        user_id: str,
        agent_id: str,
        preferences: list[tuple[str, bool]],
        tenant_id: str | None = None,
    ) -> int:
        """Set multiple preferences at once (efficient bulk operation).

        Args:
            user_id: User identifier
            agent_id: Agent identifier (required)
            preferences: List of (skill_name, enabled) tuples
            tenant_id: Optional tenant identifier

        Returns:
            Number of preferences set/updated
        """
        count = 0
        for skill_name, enabled in preferences:
            try:
                self.set_preference(
                    user_id=user_id,
                    agent_id=agent_id,
                    skill_name=skill_name,
                    enabled=enabled,
                    tenant_id=tenant_id,
                )
                count += 1
            except Exception as e:
                logger.warning(f"Failed to set preference for {skill_name}: {e}")
                continue

        return count

    def _get_preference_record(self, user_id: str, agent_id: str, skill_name: str) -> Any | None:
        """Internal method to get preference record from database.

        Args:
            user_id: User identifier
            agent_id: Agent identifier (required)
            skill_name: Name of the skill

        Returns:
            SkillUserPreferenceModel or None
        """
        from nexus.storage.models import SkillUserPreferenceModel

        return (
            self._session.query(SkillUserPreferenceModel)
            .filter(
                SkillUserPreferenceModel.user_id == user_id,
                SkillUserPreferenceModel.agent_id == agent_id,
                SkillUserPreferenceModel.skill_name == skill_name,
            )
            .first()
        )
