"""Playbook management for ACE learning system."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from nexus.core.permissions import OperationContext, Permission
from nexus.storage.models import PlaybookModel


class PlaybookManager:
    """Manage agent playbooks (learned strategies and patterns).

    Playbooks contain strategies marked as:
    - ✓ Helpful: Proven successful patterns
    - ✗ Harmful: Known failure patterns to avoid
    - ○ Neutral: Observations without clear outcome

    Enforces ReBAC permissions for all operations.
    """

    def __init__(
        self,
        session: Session,
        backend: Any,
        user_id: str,
        agent_id: str | None = None,
        tenant_id: str | None = None,
        context: OperationContext | None = None,
    ):
        """Initialize playbook manager.

        Args:
            session: Database session
            backend: Storage backend for CAS content
            user_id: User ID for ownership
            agent_id: Optional agent ID
            tenant_id: Optional tenant ID
            context: Optional operation context for permission checks
        """
        self.session = session
        self.backend = backend
        self.user_id = user_id
        self.agent_id = agent_id
        self.tenant_id = tenant_id
        self.context = context or OperationContext(
            user=user_id, groups=[], is_admin=False, is_system=False
        )

    def _check_permission(self, playbook: PlaybookModel, permission: Permission) -> bool:
        """Check if current user has permission to access playbook.

        Uses identity-based ReBAC logic:
        1. Admin/system bypass
        2. Direct creator (agent matches)
        3. User ownership (same user_id)
        4. Tenant-scoped sharing (same tenant_id and scope='tenant')
        5. Public visibility for read operations

        Args:
            playbook: Playbook model
            permission: Permission to check

        Returns:
            True if permission granted
        """
        # 1. Admin/system bypass
        if self.context.is_admin or self.context.is_system:
            return True

        # 2. Direct creator access
        if self.context.user == playbook.agent_id:
            return True

        # 3. User ownership
        if self.context.user == playbook.user_id:
            return True

        # 4. Tenant-scoped sharing
        if playbook.scope == "tenant" and playbook.tenant_id == self.tenant_id:
            return True

        # 5. Global scope or public visibility (read-only)
        if permission == Permission.READ:
            if playbook.scope == "global" or playbook.visibility == "public":
                return True
            # Shared visibility within tenant
            if playbook.visibility == "shared" and playbook.tenant_id == self.tenant_id:
                return True

        return False

    def create_playbook(
        self,
        name: str,
        description: str | None = None,
        scope: Literal["agent", "user", "tenant", "global"] = "agent",
        visibility: Literal["private", "shared", "public"] = "private",
        initial_strategies: list[dict[str, Any]] | None = None,
    ) -> str:
        """Create a new playbook.

        Args:
            name: Playbook name
            description: Optional description
            scope: Scope level ('agent', 'user', 'tenant', 'global')
            visibility: Visibility level ('private', 'shared', 'public')
            initial_strategies: Optional initial strategies

        Returns:
            playbook_id: ID of the created playbook

        Example:
            >>> playbook_id = playbook_mgr.create_playbook(
            ...     name="API Error Handling",
            ...     description="Strategies for handling API errors",
            ...     initial_strategies=[
            ...         {
            ...             "type": "helpful",
            ...             "description": "Use exponential backoff for rate limit errors",
            ...             "evidence": ["traj_123", "traj_456"]
            ...         }
            ...     ]
            ... )
        """
        playbook_id = str(uuid.uuid4())

        # Initialize playbook content
        content = {
            "strategies": initial_strategies or [],
            "created_at": datetime.now(UTC).isoformat(),
            "version": 1,
            "metadata": {},
        }

        # Store content in CAS
        content_json = json.dumps(content, indent=2).encode("utf-8")
        content_hash = self.backend.write_content(content_json)

        # Create playbook record
        playbook = PlaybookModel(
            playbook_id=playbook_id,
            user_id=self.user_id,
            agent_id=self.agent_id,
            tenant_id=self.tenant_id,
            name=name,
            description=description,
            version=1,
            content_hash=content_hash,
            scope=scope,
            visibility=visibility,
        )

        self.session.add(playbook)
        self.session.commit()

        return playbook_id

    def get_playbook(self, playbook_id: str) -> dict[str, Any] | None:
        """Get playbook by ID with full content.

        Args:
            playbook_id: Playbook ID

        Returns:
            Dictionary with playbook data and strategies, or None if not found or no permission
        """
        import logging

        logger = logging.getLogger(__name__)

        playbook = self.session.query(PlaybookModel).filter_by(playbook_id=playbook_id).first()
        if not playbook:
            return None

        # Check READ permission
        if not self._check_permission(playbook, Permission.READ):
            return None

        # Read content from CAS
        try:
            logger.info(
                f"Reading playbook content: hash={playbook.content_hash}, playbook_id={playbook_id}"
            )
            content_bytes = self.backend.read_content(playbook.content_hash)
            content_data = json.loads(content_bytes.decode("utf-8"))
        except Exception as e:
            logger.error(f"Failed to read playbook content for {playbook_id}: {e}")
            logger.error(f"Content hash: {playbook.content_hash}")
            logger.error("This usually means the CAS content was deleted but DB record remains")
            # Return minimal playbook data instead of crashing
            content_data = {
                "strategies": [],
                "created_at": playbook.created_at.isoformat() if playbook.created_at else None,
                "version": playbook.version,
                "metadata": {"error": "Content not found in backend storage"},
            }

        return {
            "playbook_id": playbook.playbook_id,
            "user_id": playbook.user_id,
            "agent_id": playbook.agent_id,
            "name": playbook.name,
            "description": playbook.description,
            "version": playbook.version,
            "scope": playbook.scope,
            "visibility": playbook.visibility,
            "usage_count": playbook.usage_count,
            "success_rate": playbook.success_rate,
            "avg_improvement": playbook.avg_improvement,
            "created_at": playbook.created_at.isoformat() if playbook.created_at else None,
            "updated_at": playbook.updated_at.isoformat() if playbook.updated_at else None,
            "last_used_at": playbook.last_used_at.isoformat() if playbook.last_used_at else None,
            "content": content_data,
        }

    def update_playbook(
        self,
        playbook_id: str,
        strategies: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        increment_version: bool = True,
    ) -> None:
        """Update playbook strategies.

        Args:
            playbook_id: Playbook ID
            strategies: New strategies to add/update
            metadata: Additional metadata to merge
            increment_version: Whether to increment version number

        Raises:
            PermissionError: If user lacks WRITE permission

        Example:
            >>> playbook_mgr.update_playbook(
            ...     playbook_id,
            ...     strategies=[
            ...         {
            ...             "type": "harmful",
            ...             "description": "AVOID aggressive caching without TTL validation",
            ...             "evidence": ["traj_789"],
            ...             "impact": "Caused 15% stale data rate"
            ...         }
            ...     ]
            ... )
        """
        playbook = self.session.query(PlaybookModel).filter_by(playbook_id=playbook_id).first()
        if not playbook:
            raise ValueError(f"Playbook {playbook_id} not found")

        # Check WRITE permission
        if not self._check_permission(playbook, Permission.WRITE):
            raise PermissionError(f"No WRITE permission for playbook {playbook_id}")

        # Read existing content
        import logging

        logger = logging.getLogger(__name__)

        try:
            logger.info(
                f"Updating playbook: reading content hash={playbook.content_hash}, playbook_id={playbook_id}"
            )
            content_bytes = self.backend.read_content(playbook.content_hash)
            content_data = json.loads(content_bytes.decode("utf-8"))
        except Exception as e:
            logger.error(f"Failed to read existing content for {playbook_id}: {e}")
            logger.warning("Recreating playbook content from scratch due to missing CAS data")
            # Recreate minimal content if CAS data is missing
            content_data = {
                "strategies": [],
                "created_at": playbook.created_at.isoformat()
                if playbook.created_at
                else datetime.now(UTC).isoformat(),
                "version": playbook.version,
                "metadata": {},
            }

        # Update strategies
        if strategies:
            content_data.setdefault("strategies", []).extend(strategies)

        # Merge metadata
        if metadata:
            content_data.setdefault("metadata", {}).update(metadata)

        # Update timestamps
        content_data["updated_at"] = datetime.now(UTC).isoformat()

        # Increment version if requested
        if increment_version:
            content_data["version"] = content_data.get("version", 1) + 1
            playbook.version += 1

        # Store updated content in CAS
        content_json = json.dumps(content_data, indent=2).encode("utf-8")
        new_content_hash = self.backend.write_content(content_json)

        # Update playbook record
        playbook.content_hash = new_content_hash
        playbook.updated_at = datetime.now(UTC)

        self.session.commit()

    def record_usage(
        self,
        playbook_id: str,
        success: bool,
        improvement_score: float | None = None,
    ) -> None:
        """Record playbook usage and update metrics.

        Args:
            playbook_id: Playbook ID
            success: Whether the usage was successful
            improvement_score: Optional improvement score (0.0-1.0)

        Example:
            >>> playbook_mgr.record_usage(playbook_id, success=True, improvement_score=0.8)
        """
        playbook = self.session.query(PlaybookModel).filter_by(playbook_id=playbook_id).first()
        if not playbook:
            raise ValueError(f"Playbook {playbook_id} not found")

        # Update usage count
        playbook.usage_count += 1

        # Update success rate (running average)
        if playbook.usage_count == 1:
            playbook.success_rate = 1.0 if success else 0.0
        else:
            # Weighted average favoring recent results
            alpha = 0.2  # Weight for new value
            playbook.success_rate = (1 - alpha) * playbook.success_rate + alpha * (
                1.0 if success else 0.0
            )

        # Update average improvement
        if improvement_score is not None:
            if playbook.usage_count == 1:
                playbook.avg_improvement = improvement_score
            else:
                alpha = 0.2
                playbook.avg_improvement = (
                    1 - alpha
                ) * playbook.avg_improvement + alpha * improvement_score

        # Update last used timestamp
        playbook.last_used_at = datetime.now(UTC)

        self.session.commit()

    def query_playbooks(
        self,
        agent_id: str | None = None,
        scope: str | None = None,
        name_pattern: str | None = None,
        limit: int = 50,
        path: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query playbooks by filters with permission checks.

        Args:
            agent_id: Filter by agent ID
            scope: Filter by scope
            name_pattern: Filter by name (SQL LIKE pattern)
            limit: Maximum results
            path: Filter by path context

        Returns:
            List of playbook summaries (without full content), filtered by permissions
        """
        query = self.session.query(PlaybookModel)

        if agent_id:
            query = query.filter_by(agent_id=agent_id)
        if scope:
            query = query.filter_by(scope=scope)
        if name_pattern:
            query = query.filter(PlaybookModel.name.like(name_pattern))
        if path:
            query = query.filter_by(path=path)

        query = query.order_by(PlaybookModel.updated_at.desc()).limit(
            limit * 2
        )  # Fetch extra for filtering
        playbooks = query.all()

        # Filter by permissions
        accessible_playbooks = [
            pb for pb in playbooks if self._check_permission(pb, Permission.READ)
        ][:limit]  # Apply limit after permission filtering

        return [
            {
                "playbook_id": p.playbook_id,
                "name": p.name,
                "description": p.description,
                "version": p.version,
                "scope": p.scope,
                "visibility": p.visibility,
                "usage_count": p.usage_count,
                "success_rate": p.success_rate,
                "avg_improvement": p.avg_improvement,
                "updated_at": p.updated_at.isoformat() if p.updated_at else None,
                "path": getattr(p, "path", None),  # Handle older DB schemas without path column
            }
            for p in accessible_playbooks
        ]

    def delete_playbook(self, playbook_id: str) -> bool:
        """Delete a playbook.

        Args:
            playbook_id: Playbook ID

        Returns:
            True if deleted, False if not found
        """
        playbook = self.session.query(PlaybookModel).filter_by(playbook_id=playbook_id).first()
        if not playbook:
            return False

        self.session.delete(playbook)
        self.session.commit()
        return True

    def get_relevant_strategies(
        self,
        playbook_id: str,
        task_description: str,
        strategy_type: Literal["helpful", "harmful", "neutral"] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Get relevant strategies from playbook for a task.

        Args:
            playbook_id: Playbook ID
            task_description: Description of the task
            strategy_type: Filter by strategy type
            limit: Maximum strategies to return

        Returns:
            List of relevant strategies

        Note:
            This is a simple implementation. For production, this should use
            semantic search with embeddings to find truly relevant strategies.
        """
        playbook_data = self.get_playbook(playbook_id)
        if not playbook_data:
            return []

        strategies = playbook_data["content"].get("strategies", [])

        # Filter by type if specified
        if strategy_type:
            strategies = [s for s in strategies if s.get("type") == strategy_type]

        # Simple keyword matching (TODO: replace with semantic search)
        task_lower = task_description.lower()
        scored_strategies = []

        for strategy in strategies:
            desc = strategy.get("description", "").lower()
            score = 0.0

            # Simple relevance scoring
            if any(word in desc for word in task_lower.split()):
                score = 1.0

            if score > 0:
                scored_strategies.append((score, strategy))

        # Sort by score and limit
        scored_strategies.sort(key=lambda x: x[0], reverse=True)
        return [s for _, s in scored_strategies[:limit]]
