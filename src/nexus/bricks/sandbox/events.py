"""Agent lifecycle event log (Issue #1307).

Append-only audit log for agent lifecycle events such as sandbox creation,
connection, and termination.  Used by ``SandboxAuthService`` to record
sandbox lifecycle events as required by the acceptance criteria.

Design decisions:
    - #7B: Simple audit table (not EventBus — that only supports FileEvent)
    - #16C: Synchronous inserts (lifecycle events are low-frequency)
    - Session-per-operation pattern (same as AgentRegistry)
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from nexus.storage.models import AgentEventModel

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)


class AgentEventLog:
    """Append-only agent lifecycle event log.

    Thread-safe via session-per-operation pattern (each call creates and
    closes its own session).

    Args:
        session_factory: SQLAlchemy sessionmaker for database access.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @contextmanager
    def _get_session(self) -> Generator[Session, None, None]:
        """Create a session with auto-commit/rollback."""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def record(
        self,
        agent_id: str,
        event_type: str,
        zone_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        """Record a lifecycle event.

        Args:
            agent_id: Agent that the event pertains to.
            event_type: Event type string (e.g. "sandbox.created", "sandbox.stopped").
            zone_id: Optional zone context.
            payload: Optional event-specific data (serialized as JSON).

        Returns:
            The generated event ID (UUID).
        """
        event_id = str(uuid.uuid4())
        now = datetime.now(UTC)

        model = AgentEventModel(
            id=event_id,
            agent_id=agent_id,
            event_type=event_type,
            zone_id=zone_id,
            payload=json.dumps(payload) if payload else None,
            created_at=now,
        )

        with self._get_session() as session:
            session.add(model)

        logger.debug(
            "[AGENT-EVENT] Recorded %s for agent %s (id=%s)",
            event_type,
            agent_id,
            event_id,
        )
        return event_id

    def list_events(
        self,
        agent_id: str,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List events for an agent, newest first.

        Args:
            agent_id: Agent identifier.
            event_type: Optional filter by event type.
            limit: Maximum number of events to return.

        Returns:
            List of event dicts with id, agent_id, event_type, zone_id,
            payload, created_at.
        """
        with self._get_session() as session:
            stmt = (
                select(AgentEventModel)
                .where(AgentEventModel.agent_id == agent_id)
                .order_by(AgentEventModel.created_at.desc())
                .limit(limit)
            )
            if event_type is not None:
                stmt = stmt.where(AgentEventModel.event_type == event_type)

            models = list(session.execute(stmt).scalars().all())

        return [self._model_to_dict(m) for m in models]

    @staticmethod
    def _model_to_dict(model: AgentEventModel) -> dict[str, Any]:
        """Convert ORM model to plain dict."""
        payload: dict[str, Any] | None = None
        if model.payload:
            try:
                payload = json.loads(model.payload)
            except (json.JSONDecodeError, TypeError):
                logger.warning("[AGENT-EVENT] Corrupt payload for event %s", model.id)

        return {
            "id": model.id,
            "agent_id": model.agent_id,
            "event_type": model.event_type,
            "zone_id": model.zone_id,
            "payload": payload,
            "created_at": model.created_at.isoformat() if model.created_at else None,
        }
