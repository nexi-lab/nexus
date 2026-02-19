"""Sandbox metadata repository (Issue #2051: Decompose SandboxManager).

Encapsulates all database operations for sandbox metadata behind a
consistent interface. Uses session-per-operation pattern for safety.

Each method creates a fresh session from the factory, preventing stale
identity maps and connection leaks.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.exc import PendingRollbackError, SQLAlchemyError
from sqlalchemy.orm import Session

from nexus.bricks.sandbox.sandbox_provider import SandboxNotFoundError
from nexus.storage.models import SandboxMetadataModel

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class SandboxRepository:
    """Database repository for sandbox metadata (CRUD + queries).

    All methods are synchronous and use the session-per-operation pattern.
    Each call creates and closes its own session from the factory.

    Args:
        session_factory: Callable that creates fresh SQLAlchemy sessions.
    """

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    @contextmanager
    def _get_session(self) -> Generator[Session, None, None]:
        """Create a fresh session for a single DB operation."""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _execute_with_retry(
        self, operation: Callable[[Session], _T], context: str = "query"
    ) -> _T:
        """Execute a database operation with one retry on PendingRollbackError.

        Each attempt gets a fresh session from the factory.

        Args:
            operation: Callable that accepts a Session and returns a value.
            context: Human-readable label for log messages.

        Returns:
            Whatever ``operation`` returns.

        Raises:
            SQLAlchemyError: If the retry also fails.
        """
        try:
            with self._get_session() as session:
                return operation(session)
        except (PendingRollbackError, SQLAlchemyError) as exc:
            logger.warning("Database error during %s: %s", context, exc)
            try:
                with self._get_session() as session:
                    return operation(session)
            except SQLAlchemyError as retry_exc:
                logger.error("Database error persisted after retry: %s", retry_exc)
                raise

    def get_metadata(self, sandbox_id: str) -> dict[str, Any]:
        """Get sandbox metadata from database as a dict.

        Returns a dict to avoid detached ORM object issues across sessions.
        Conversion to dict happens INSIDE the session to prevent
        DetachedInstanceError.

        Args:
            sandbox_id: Sandbox ID.

        Returns:
            Sandbox metadata dict with all fields.

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist.
        """

        def _query(session: Session) -> dict[str, Any] | None:
            result = session.execute(
                select(SandboxMetadataModel).where(
                    SandboxMetadataModel.sandbox_id == sandbox_id
                )
            )
            metadata = result.scalar_one_or_none()
            if metadata is None:
                return None
            return self._metadata_to_dict(metadata)

        result = self._execute_with_retry(_query, context="metadata lookup")

        if result is None:
            raise SandboxNotFoundError(f"Sandbox {sandbox_id} not found")

        return result

    def get_metadata_field(self, sandbox_id: str, field: str) -> Any:
        """Get a single field from sandbox metadata.

        Args:
            sandbox_id: Sandbox ID.
            field: Attribute name on SandboxMetadataModel.

        Returns:
            The field value.

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist.
        """

        def _query(session: Session) -> Any:
            result = session.execute(
                select(SandboxMetadataModel).where(
                    SandboxMetadataModel.sandbox_id == sandbox_id
                )
            )
            metadata = result.scalar_one_or_none()
            if not metadata:
                raise SandboxNotFoundError(f"Sandbox {sandbox_id} not found")
            return getattr(metadata, field)

        return self._execute_with_retry(_query, context=f"metadata field {field}")

    def update_metadata(self, sandbox_id: str, **updates: Any) -> dict[str, Any]:
        """Re-query and update metadata fields in a fresh session.

        Args:
            sandbox_id: Sandbox ID.
            **updates: Field name -> value pairs to update.

        Returns:
            Updated metadata dict.

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist.
        """
        with self._get_session() as session:
            metadata = session.execute(
                select(SandboxMetadataModel).where(
                    SandboxMetadataModel.sandbox_id == sandbox_id
                )
            ).scalar_one_or_none()
            if not metadata:
                raise SandboxNotFoundError(f"Sandbox {sandbox_id} not found")
            for key, value in updates.items():
                setattr(metadata, key, value)
            session.flush()
            session.refresh(metadata)
            return self._metadata_to_dict(metadata)

    def create_metadata(
        self,
        sandbox_id: str,
        name: str,
        user_id: str,
        zone_id: str,
        agent_id: str | None,
        provider: str,
        template_id: str | None,
        created_at: datetime,
        last_active_at: datetime,
        ttl_minutes: int,
        expires_at: datetime,
    ) -> dict[str, Any]:
        """Create a new sandbox metadata record.

        Args:
            sandbox_id: Sandbox ID from provider.
            name: User-friendly name.
            user_id: Owner user ID.
            zone_id: Zone ID.
            agent_id: Optional agent ID.
            provider: Provider name.
            template_id: Provider template ID.
            created_at: Creation timestamp.
            last_active_at: Last activity timestamp.
            ttl_minutes: TTL in minutes.
            expires_at: Expiry timestamp.

        Returns:
            Created metadata dict.

        Raises:
            SQLAlchemyError: If the insert fails.
        """
        with self._get_session() as session:
            metadata = SandboxMetadataModel(
                sandbox_id=sandbox_id,
                name=name,
                user_id=user_id,
                agent_id=agent_id,
                zone_id=zone_id,
                provider=provider,
                template_id=template_id,
                status="active",
                created_at=created_at,
                last_active_at=last_active_at,
                ttl_minutes=ttl_minutes,
                expires_at=expires_at,
                auto_created=1,
            )
            session.add(metadata)
            session.flush()
            session.refresh(metadata)
            return self._metadata_to_dict(metadata)

    def find_active_by_name(
        self, user_id: str, name: str
    ) -> dict[str, Any] | None:
        """Find an active sandbox by user and name.

        Args:
            user_id: User ID.
            name: Sandbox name.

        Returns:
            Sandbox metadata dict if found, None otherwise.
        """

        def _query(session: Session) -> dict[str, Any] | None:
            result = session.execute(
                select(SandboxMetadataModel).where(
                    SandboxMetadataModel.user_id == user_id,
                    SandboxMetadataModel.name == name,
                    SandboxMetadataModel.status == "active",
                )
            )
            metadata = result.scalar_one_or_none()
            if metadata:
                return self._metadata_to_dict(metadata)
            return None

        return self._execute_with_retry(_query, context="sandbox lookup")

    def list_sandboxes(
        self,
        user_id: str | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List sandboxes with optional filtering.

        Args:
            user_id: Filter by user.
            zone_id: Filter by zone.
            agent_id: Filter by agent.
            status: Filter by status.

        Returns:
            List of sandbox metadata dicts.
        """
        query = select(SandboxMetadataModel)
        if user_id:
            query = query.where(SandboxMetadataModel.user_id == user_id)
        if zone_id:
            query = query.where(SandboxMetadataModel.zone_id == zone_id)
        if agent_id:
            query = query.where(SandboxMetadataModel.agent_id == agent_id)
        if status:
            query = query.where(SandboxMetadataModel.status == status)

        def _list_query(session: Session) -> list[dict[str, Any]]:
            sandboxes = list(session.execute(query).scalars().all())
            return [self._metadata_to_dict(sb) for sb in sandboxes]

        return self._execute_with_retry(_list_query, context="sandbox list")

    def find_expired(self) -> list[str]:
        """Find IDs of active sandboxes that have expired.

        Returns:
            List of expired sandbox IDs.
        """
        now = datetime.now(UTC)

        def _find(session: Session) -> list[str]:
            result = session.execute(
                select(SandboxMetadataModel.sandbox_id).where(
                    SandboxMetadataModel.status == "active",
                    SandboxMetadataModel.expires_at < now,
                )
            )
            return list(result.scalars().all())

        try:
            return self._execute_with_retry(_find, context="expired sandbox query")
        except SQLAlchemyError:
            return []

    @staticmethod
    def _metadata_to_dict(metadata: SandboxMetadataModel) -> dict[str, Any]:
        """Convert metadata model to dict.

        Must be called while the session that loaded the model is still open.

        Args:
            metadata: Sandbox metadata model.

        Returns:
            Metadata dict.
        """
        created_at = metadata.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)

        return {
            "sandbox_id": metadata.sandbox_id,
            "name": metadata.name,
            "user_id": metadata.user_id,
            "agent_id": metadata.agent_id,
            "zone_id": metadata.zone_id,
            "provider": metadata.provider,
            "template_id": metadata.template_id,
            "status": metadata.status,
            "created_at": metadata.created_at.isoformat(),
            "last_active_at": metadata.last_active_at.isoformat(),
            "paused_at": metadata.paused_at.isoformat()
            if metadata.paused_at
            else None,
            "stopped_at": metadata.stopped_at.isoformat()
            if metadata.stopped_at
            else None,
            "ttl_minutes": metadata.ttl_minutes,
            "expires_at": metadata.expires_at.isoformat()
            if metadata.expires_at
            else None,
            "uptime_seconds": (datetime.now(UTC) - created_at).total_seconds(),
        }
