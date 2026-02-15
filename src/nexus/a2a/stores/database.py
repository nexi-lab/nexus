"""SQLAlchemy-backed task store.

Wraps sync session calls in ``asyncio.to_thread()`` to avoid blocking
the event loop (Decision 14).  Deduplicates serialization logic via
``_task_to_db_dict()`` helper (Decision 5).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from nexus.a2a.models import (
    Artifact,
    Message,
    Task,
    TaskState,
    TaskStatus,
)

logger = logging.getLogger(__name__)


class DatabaseTaskStore:
    """SQLAlchemy-backed task store.

    Parameters
    ----------
    session_factory:
        A callable that returns a SQLAlchemy ``Session`` (sync).
    """

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def save(
        self,
        task: Task,
        *,
        zone_id: str,
        agent_id: str | None = None,
    ) -> None:
        def _do_save() -> None:
            from nexus.a2a.db import A2ATaskModel

            session = self._session_factory()
            try:
                existing = session.get(A2ATaskModel, task.id)
                if existing is not None and existing.zone_id == zone_id:
                    # Update
                    db_dict = _task_to_db_dict(task)
                    existing.state = db_dict["state"]
                    existing.messages_json = db_dict["messages_json"]
                    existing.artifacts_json = db_dict["artifacts_json"]
                    existing.metadata_json = db_dict["metadata_json"]
                    existing.agent_id = agent_id
                    session.commit()
                else:
                    # Insert
                    db_dict = _task_to_db_dict(task)
                    model = A2ATaskModel(
                        id=task.id,
                        context_id=task.contextId,
                        zone_id=zone_id,
                        agent_id=agent_id,
                        **db_dict,
                    )
                    session.add(model)
                    session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        await asyncio.to_thread(_do_save)

    async def get(self, task_id: str, *, zone_id: str) -> Task | None:
        def _do_get() -> Task | None:
            from nexus.a2a.db import A2ATaskModel

            session = self._session_factory()
            try:
                row = session.get(A2ATaskModel, task_id)
                if row is None or row.zone_id != zone_id:
                    return None
                return _db_row_to_task(row)
            finally:
                session.close()

        return await asyncio.to_thread(_do_get)

    async def delete(self, task_id: str, *, zone_id: str) -> bool:
        def _do_delete() -> bool:
            from nexus.a2a.db import A2ATaskModel

            session = self._session_factory()
            try:
                row = session.get(A2ATaskModel, task_id)
                if row is None or row.zone_id != zone_id:
                    return False
                session.delete(row)
                session.commit()
                return True
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        return await asyncio.to_thread(_do_delete)

    async def list_tasks(
        self,
        *,
        zone_id: str,
        agent_id: str | None = None,
        state: TaskState | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        def _do_list() -> list[Task]:
            from sqlalchemy import select

            from nexus.a2a.db import A2ATaskModel

            session = self._session_factory()
            try:
                stmt = (
                    select(A2ATaskModel)
                    .where(A2ATaskModel.zone_id == zone_id)
                    .order_by(A2ATaskModel.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
                if agent_id is not None:
                    stmt = stmt.where(A2ATaskModel.agent_id == agent_id)
                if state is not None:
                    stmt = stmt.where(A2ATaskModel.state == state.value)

                rows = session.execute(stmt).scalars().all()
                return [_db_row_to_task(row) for row in rows]
            finally:
                session.close()

        return await asyncio.to_thread(_do_list)


# ======================================================================
# Serialization helpers (Decision 5: DRY)
# ======================================================================


def _task_to_db_dict(task: Task) -> dict[str, Any]:
    """Convert a Task to the dict of DB column values.

    Single source of truth for Task → DB serialization.
    """
    return {
        "state": task.status.state.value,
        "messages_json": json.dumps([m.model_dump(mode="json") for m in task.history]),
        "artifacts_json": json.dumps([a.model_dump(mode="json") for a in task.artifacts]),
        "metadata_json": json.dumps(task.metadata) if task.metadata else None,
    }


def _db_row_to_task(row: Any) -> Task:
    """Convert a database row to a Task model."""
    messages = json.loads(row.messages_json) if row.messages_json else []
    artifacts = json.loads(row.artifacts_json) if row.artifacts_json else []
    metadata = json.loads(row.metadata_json) if row.metadata_json else None

    return Task(
        id=row.id,
        contextId=row.context_id,
        status=TaskStatus(
            state=TaskState(row.state),
            timestamp=row.updated_at,
        ),
        history=[Message.model_validate(m) for m in messages],
        artifacts=[Artifact.model_validate(a) for a in artifacts],
        metadata=metadata,
    )
