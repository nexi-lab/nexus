"""Factory adapters — NexusFSFileReader, WorkflowLifecycleAdapter."""

from typing import Any

# =========================================================================
# Issue #1520: NexusFS → FileReaderProtocol adapter
# =========================================================================


class _NexusFSFileReader:
    """Adapts a NexusFS instance to the FileReaderProtocol interface.

    This adapter is the sole coupling point between the kernel (NexusFS)
    and the search brick. Search modules never import NexusFS directly;
    they receive a FileReaderProtocol at composition time.

    Usage::

        from nexus.factory.adapters import _NexusFSFileReader

        reader = _NexusFSFileReader(nexus_fs_instance)
        content = reader.read_text("/path/to/file.py")
    """

    def __init__(self, nx: Any) -> None:
        self._nx = nx

    async def read_text(self, path: str) -> str:
        # Read with admin context so the search daemon can index all files
        # regardless of per-user ReBAC permissions.
        from nexus.contracts.types import OperationContext

        admin_ctx = OperationContext(
            user_id="system",
            groups=[],
            is_admin=True,
            is_system=True,
        )
        content_raw = await self._nx.sys_read(path, context=admin_ctx)
        if isinstance(content_raw, bytes):
            return content_raw.decode("utf-8", errors="ignore")
        return str(content_raw)

    def get_searchable_text(self, path: str) -> str | None:
        result: str | None = self._nx.metadata.get_searchable_text(path)
        return result

    async def list_files(self, path: str, recursive: bool = True) -> list[Any]:
        # Read with admin context so the search daemon can index all files
        # regardless of per-user ReBAC permissions (same as read_text).
        from nexus.contracts.types import OperationContext

        admin_ctx = OperationContext(
            user_id="system",
            groups=[],
            is_admin=True,
            is_system=True,
        )
        result = await self._nx.sys_readdir(path, recursive=recursive, context=admin_ctx)
        items: list[Any] = result.items if hasattr(result, "items") else result
        return items

    def get_session(self) -> Any:
        return self._nx.SessionLocal()

    def get_path_id(self, path: str, session: Any = None) -> str | None:
        """Look up path_id, optionally reusing an existing session."""
        from sqlalchemy import select

        from nexus.storage.models import FilePathModel

        stmt = select(FilePathModel.path_id).where(
            FilePathModel.virtual_path == path,
            FilePathModel.deleted_at.is_(None),
        )
        if session is not None:
            path_id: str | None = session.execute(stmt).scalar_one_or_none()
            return path_id
        with self._nx.SessionLocal() as s:
            path_id = s.execute(stmt).scalar_one_or_none()
            return path_id

    def get_content_hash(self, path: str, session: Any = None) -> str | None:
        """Look up content_hash, optionally reusing an existing session."""
        from sqlalchemy import select

        from nexus.storage.models import FilePathModel

        stmt = select(FilePathModel.content_hash).where(
            FilePathModel.virtual_path == path,
            FilePathModel.deleted_at.is_(None),
        )
        if session is not None:
            content_hash: str | None = session.execute(stmt).scalar_one_or_none()
            return content_hash
        with self._nx.SessionLocal() as s:
            content_hash = s.execute(stmt).scalar_one_or_none()
            return content_hash


# ---------------------------------------------------------------------------
# Issue #1704: WorkflowEngine lifecycle adapter
# ---------------------------------------------------------------------------


class _WorkflowLifecycleAdapter:
    """Adapter: BrickLifecycleProtocol -> WorkflowEngine.

    WorkflowEngine exposes ``startup()`` but BrickLifecycleManager expects
    ``start()``.  This thin adapter bridges the naming mismatch.
    """

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    async def start(self) -> None:
        if hasattr(self._engine, "startup"):
            await self._engine.startup()

    async def stop(self) -> None:
        pass  # WorkflowEngine has no explicit shutdown

    async def health_check(self) -> bool:
        if hasattr(self._engine, "health_check"):
            result: bool = await self._engine.health_check()
            return result
        return self._engine is not None
