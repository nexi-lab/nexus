"""SQLAlchemy-backed task store.

Wraps sync session calls in a dedicated ``ThreadPoolExecutor`` to avoid
blocking the event loop (Decision 14).  Serialization logic is delegated
to ``serialization.py`` (Decision 3).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from sqlalchemy.exc import SQLAlchemyError

from nexus.a2a.models import Task, TaskState
from nexus.a2a.stores.serialization import task_from_db_row, task_to_db_columns

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class DatabaseTaskStore:
    """SQLAlchemy-backed task store.

    .. deprecated::
        DatabaseTaskStore is deprecated.  Use ``VFSTaskStore`` for
        filesystem-backed persistence (§17.6 convergence).  This store
        will be removed in a future release.

    Parameters
    ----------
    session_factory:
        A callable that returns a SQLAlchemy ``Session`` (sync).
    executor:
        Optional ``ThreadPoolExecutor`` for DB operations.  When *None*
        (the default) a per-instance pool is created (Decision 14).
    """

    def __init__(
        self,
        session_factory: Any,
        *,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        import warnings

        warnings.warn(
            "DatabaseTaskStore is deprecated. Use VFSTaskStore for "
            "filesystem-backed persistence. This store will be removed "
            "in a future release.",
            DeprecationWarning,
            stacklevel=2,
        )
        self._session_factory = session_factory
        self._executor = executor or ThreadPoolExecutor(max_workers=20, thread_name_prefix="a2a-db")

    async def _run_in_session(self, fn: Callable[..., _T]) -> _T:
        """Run a sync function in the dedicated DB thread pool.

        The function receives a SQLAlchemy session and is responsible
        for committing/rolling back as needed.  The session is always
        closed in a ``finally`` block.
        """
        import asyncio

        loop = asyncio.get_running_loop()

        def _wrapper() -> _T:
            session = self._session_factory()
            try:
                return fn(session)
            finally:
                session.close()

        return await loop.run_in_executor(self._executor, _wrapper)

    async def save(
        self,
        task: Task,
        *,
        zone_id: str,
        agent_id: str | None = None,
    ) -> None:
        def _do_save(session: Any) -> None:
            from nexus.a2a.db import A2ATaskModel

            existing = session.get(A2ATaskModel, task.id)
            if existing is not None and existing.zone_id == zone_id:
                db_cols = task_to_db_columns(task)
                existing.state = db_cols["state"]
                existing.messages_json = db_cols["messages_json"]
                existing.artifacts_json = db_cols["artifacts_json"]
                existing.metadata_json = db_cols["metadata_json"]
                existing.agent_id = agent_id
                try:
                    session.commit()
                except SQLAlchemyError:
                    session.rollback()
                    raise
            else:
                db_cols = task_to_db_columns(task)
                model = A2ATaskModel(
                    id=task.id,
                    context_id=task.contextId,
                    zone_id=zone_id,
                    agent_id=agent_id,
                    **db_cols,
                )
                session.add(model)
                try:
                    session.commit()
                except SQLAlchemyError:
                    session.rollback()
                    raise

        await self._run_in_session(_do_save)

    async def get(self, task_id: str, *, zone_id: str) -> Task | None:
        def _do_get(session: Any) -> Task | None:
            from nexus.a2a.db import A2ATaskModel

            row = session.get(A2ATaskModel, task_id)
            if row is None or row.zone_id != zone_id:
                return None
            return task_from_db_row(row)

        return await self._run_in_session(_do_get)

    async def delete(self, task_id: str, *, zone_id: str) -> bool:
        def _do_delete(session: Any) -> bool:
            from nexus.a2a.db import A2ATaskModel

            row = session.get(A2ATaskModel, task_id)
            if row is None or row.zone_id != zone_id:
                return False
            session.delete(row)
            try:
                session.commit()
            except SQLAlchemyError:
                session.rollback()
                raise
            return True

        return await self._run_in_session(_do_delete)

    async def list_tasks(
        self,
        *,
        zone_id: str,
        agent_id: str | None = None,
        state: TaskState | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        def _do_list(session: Any) -> list[Task]:
            from sqlalchemy import select

            from nexus.a2a.db import A2ATaskModel

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
            return [task_from_db_row(row) for row in rows]

        return await self._run_in_session(_do_list)
