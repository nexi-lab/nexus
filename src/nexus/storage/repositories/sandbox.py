"""SQLAlchemy-backed sandbox metadata repository.

Concrete implementation of SandboxRepositoryProtocol defined in
bricks/sandbox/protocols.py. Handles all database operations for
sandbox metadata persistence.

Issue #2189: Extracted from bricks/sandbox/repository.py.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar

from sqlalchemy import select
from sqlalchemy.exc import PendingRollbackError, SQLAlchemyError
from sqlalchemy.orm import Session

from nexus.bricks.sandbox.sandbox_provider import SandboxNotFoundError

if TYPE_CHECKING:
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class SQLAlchemySandboxRepository:
    """Database repository for sandbox metadata (CRUD + queries).

    Satisfies SandboxRepositoryProtocol via structural subtyping.
    All methods are synchronous and use the session-per-operation pattern.

    Args:
        record_store: RecordStoreABC providing database access.
    """

    def __init__(self, record_store: RecordStoreABC) -> None:
        self._session_factory = record_store.session_factory

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

    def _execute_with_retry(self, operation: Callable[[Session], _T], context: str = "query") -> _T:
        """Execute a database operation with one retry on PendingRollbackError."""
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
        """Get sandbox metadata from database as a dict."""
        from nexus.storage.models import SandboxMetadataModel

        def _query(session: Session) -> dict[str, Any] | None:
            result = session.execute(
                select(SandboxMetadataModel).where(SandboxMetadataModel.sandbox_id == sandbox_id)
            )
            metadata = result.scalar_one_or_none()
            if metadata is None:
                return None
            return _metadata_to_dict(metadata)

        result = self._execute_with_retry(_query, context="metadata lookup")

        if result is None:
            raise SandboxNotFoundError(f"Sandbox {sandbox_id} not found")

        return result

    def get_metadata_field(self, sandbox_id: str, field: str) -> Any:
        """Get a single field from sandbox metadata."""
        from nexus.storage.models import SandboxMetadataModel

        def _query(session: Session) -> Any:
            result = session.execute(
                select(SandboxMetadataModel).where(SandboxMetadataModel.sandbox_id == sandbox_id)
            )
            metadata = result.scalar_one_or_none()
            if not metadata:
                raise SandboxNotFoundError(f"Sandbox {sandbox_id} not found")
            return getattr(metadata, field)

        return self._execute_with_retry(_query, context=f"metadata field {field}")

    def update_metadata(self, sandbox_id: str, **updates: Any) -> dict[str, Any]:
        """Re-query and update metadata fields in a fresh session."""
        from nexus.storage.models import SandboxMetadataModel

        with self._get_session() as session:
            metadata = session.execute(
                select(SandboxMetadataModel).where(SandboxMetadataModel.sandbox_id == sandbox_id)
            ).scalar_one_or_none()
            if not metadata:
                raise SandboxNotFoundError(f"Sandbox {sandbox_id} not found")
            for key, value in updates.items():
                setattr(metadata, key, value)
            session.flush()
            session.refresh(metadata)
            return _metadata_to_dict(metadata)

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
        """Create a new sandbox metadata record."""
        from nexus.storage.models import SandboxMetadataModel

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
            return _metadata_to_dict(metadata)

    def find_active_by_name(self, user_id: str, name: str) -> dict[str, Any] | None:
        """Find an active sandbox by user and name."""
        from nexus.storage.models import SandboxMetadataModel

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
                return _metadata_to_dict(metadata)
            return None

        return self._execute_with_retry(_query, context="sandbox lookup")

    def list_sandboxes(
        self,
        user_id: str | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List sandboxes with optional filtering."""
        from nexus.storage.models import SandboxMetadataModel

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
            return [_metadata_to_dict(sb) for sb in sandboxes]

        return self._execute_with_retry(_list_query, context="sandbox list")

    def find_expired(self) -> list[str]:
        """Find IDs of active sandboxes that have expired."""
        from nexus.storage.models import SandboxMetadataModel

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


def _metadata_to_dict(metadata: Any) -> dict[str, Any]:
    """Convert metadata model to dict.

    Must be called while the session that loaded the model is still open.
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
        "paused_at": metadata.paused_at.isoformat() if metadata.paused_at else None,
        "stopped_at": metadata.stopped_at.isoformat() if metadata.stopped_at else None,
        "ttl_minutes": metadata.ttl_minutes,
        "expires_at": metadata.expires_at.isoformat() if metadata.expires_at else None,
        "uptime_seconds": (datetime.now(UTC) - created_at).total_seconds(),
    }
