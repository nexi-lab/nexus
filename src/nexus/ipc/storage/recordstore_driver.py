"""RecordStore-backed storage driver for IPC messages.

Service-layer adapter that implements ``IPCStorageDriver`` by delegating
to RecordStoreABC's session factory.  All SQL access goes through
SQLAlchemy ORM (``IPCMessageModel``) — no raw asyncpg or SQL strings.

Architecture layer: **Service** (consumes RecordStoreABC, not a pillar driver).
Same pattern as the former ``PGEventLog`` (removed in Issue #1241).

Issue: #1243, #1469
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult

from nexus.storage.models.ipc_message import IPCMessageModel

if TYPE_CHECKING:
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


def _parent_dir(path: str) -> str:
    """Extract parent directory from a path."""
    parts = path.rstrip("/").rsplit("/", 1)
    if len(parts) <= 1:
        return "/"
    return parts[0] if parts[0] else "/"


def _basename(path: str) -> str:
    """Extract filename/dirname from a path."""
    return path.rstrip("/").rsplit("/", 1)[-1]


class RecordStoreStorageDriver:
    """Stores IPC messages via RecordStoreABC.

    Accepts a sync SQLAlchemy session factory (from
    ``RecordStoreABC.session_factory``) and wraps calls in
    ``asyncio.to_thread()`` to keep the ``IPCStorageDriver`` async
    interface non-blocking.

    Args:
        record_store: RecordStoreABC for database access.
    """

    def __init__(self, record_store: "RecordStoreABC") -> None:
        self._session_factory = record_store.session_factory

    async def read(self, path: str, zone_id: str) -> bytes:
        def _read() -> bytes:
            with self._session_factory() as session:
                stmt = select(IPCMessageModel.data).where(
                    IPCMessageModel.zone_id == zone_id,
                    IPCMessageModel.path == path,
                    IPCMessageModel.is_dir.is_(False),
                )
                row = session.execute(stmt).first()

            if row is None:
                raise FileNotFoundError(f"No such file: {path}")
            return bytes(row[0])

        return await asyncio.to_thread(_read)

    async def write(self, path: str, data: bytes, zone_id: str) -> None:
        dir_path = _parent_dir(path)
        filename = _basename(path)

        def _write() -> None:
            with self._session_factory() as session:
                existing = session.execute(
                    select(IPCMessageModel).where(
                        IPCMessageModel.zone_id == zone_id,
                        IPCMessageModel.path == path,
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    existing.data = data
                else:
                    session.add(
                        IPCMessageModel(
                            zone_id=zone_id,
                            path=path,
                            dir_path=dir_path,
                            filename=filename,
                            data=data,
                            is_dir=False,
                        )
                    )
                session.commit()

        await asyncio.to_thread(_write)

    async def list_dir(self, path: str, zone_id: str) -> list[str]:
        normalized = path.rstrip("/")

        def _list() -> list[str]:
            with self._session_factory() as session:
                stmt = (
                    select(IPCMessageModel.filename)
                    .where(
                        IPCMessageModel.zone_id == zone_id,
                        IPCMessageModel.dir_path == normalized,
                    )
                    .order_by(IPCMessageModel.filename)
                )
                rows = session.execute(stmt).all()
            return [row[0] for row in rows]

        results = await asyncio.to_thread(_list)

        # Lazy exists check: only verify directory marker when listing is empty
        # (common case — non-empty directory — needs only 1 query)
        if not results and not await self._dir_exists(normalized, zone_id):
            raise FileNotFoundError(f"No such directory: {path}")

        return results

    async def count_dir(self, path: str, zone_id: str) -> int:
        normalized = path.rstrip("/")

        def _count() -> int:
            with self._session_factory() as session:
                stmt = (
                    select(func.count())
                    .select_from(IPCMessageModel)
                    .where(
                        IPCMessageModel.zone_id == zone_id,
                        IPCMessageModel.dir_path == normalized,
                        IPCMessageModel.is_dir.is_(False),
                    )
                )
                result = session.execute(stmt).scalar()
            return result or 0

        count = await asyncio.to_thread(_count)

        # Lazy exists check: only verify directory marker when count is zero
        if count == 0 and not await self._dir_exists(normalized, zone_id):
            raise FileNotFoundError(f"No such directory: {path}")

        return count

    async def rename(self, src: str, dst: str, zone_id: str) -> None:
        dst_dir = _parent_dir(dst)
        dst_name = _basename(dst)

        def _rename() -> int:
            with self._session_factory() as session:
                stmt = (
                    update(IPCMessageModel)
                    .where(
                        IPCMessageModel.zone_id == zone_id,
                        IPCMessageModel.path == src,
                    )
                    .values(path=dst, dir_path=dst_dir, filename=dst_name)
                )
                result = cast(CursorResult[Any], session.execute(stmt))
                session.commit()
                return int(result.rowcount)

        rows_updated = await asyncio.to_thread(_rename)
        if rows_updated == 0:
            raise FileNotFoundError(f"No such file: {src}")

    async def mkdir(self, path: str, zone_id: str) -> None:
        normalized = path.rstrip("/")
        dir_path = _parent_dir(normalized)
        filename = _basename(normalized)

        def _mkdir() -> None:
            with self._session_factory() as session:
                existing = session.execute(
                    select(IPCMessageModel.id)
                    .where(
                        IPCMessageModel.zone_id == zone_id,
                        IPCMessageModel.path == normalized,
                    )
                    .limit(1)
                ).first()
                if existing is None:
                    session.add(
                        IPCMessageModel(
                            zone_id=zone_id,
                            path=normalized,
                            dir_path=dir_path,
                            filename=filename,
                            data=b"",
                            is_dir=True,
                        )
                    )
                    session.commit()

        await asyncio.to_thread(_mkdir)

    async def exists(self, path: str, zone_id: str) -> bool:
        normalized = path.rstrip("/")

        def _exists() -> bool:
            with self._session_factory() as session:
                stmt = (
                    select(IPCMessageModel.id)
                    .where(
                        IPCMessageModel.zone_id == zone_id,
                        IPCMessageModel.path == normalized,
                    )
                    .limit(1)
                )
                row = session.execute(stmt).first()
            return row is not None

        return await asyncio.to_thread(_exists)

    async def _dir_exists(self, normalized_path: str, zone_id: str) -> bool:
        """Check if a directory marker exists."""

        def _check() -> bool:
            with self._session_factory() as session:
                stmt = (
                    select(IPCMessageModel.id)
                    .where(
                        IPCMessageModel.zone_id == zone_id,
                        IPCMessageModel.path == normalized_path,
                        IPCMessageModel.is_dir.is_(True),
                    )
                    .limit(1)
                )
                row = session.execute(stmt).first()
            return row is not None

        return await asyncio.to_thread(_check)
