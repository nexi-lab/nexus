"""SQLAlchemy-backed agent event log repository.

Concrete implementation of AgentEventLogProtocol defined in
bricks/sandbox/protocols.py. Handles append-only event recording
for agent lifecycle events.

Issue #2189: Extracted from bricks/sandbox/events.py.
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

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


class SQLAlchemyAgentEventLog:
    """Append-only agent lifecycle event log.

    Satisfies AgentEventLogProtocol via structural subtyping.
    Thread-safe via session-per-operation pattern.

    Args:
        record_store: RecordStoreABC providing database access.
    """

    def __init__(self, record_store: RecordStoreABC) -> None:
        self._session_factory = record_store.session_factory

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
        """Record a lifecycle event. Returns the generated event ID."""
        from nexus.storage.models import AgentEventModel

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
        """List events for an agent, newest first."""
        from nexus.storage.models import AgentEventModel

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

        return [_model_to_dict(m) for m in models]


def _model_to_dict(model: Any) -> dict[str, Any]:
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
