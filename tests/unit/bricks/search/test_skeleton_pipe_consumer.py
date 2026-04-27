"""Tests for DT_PIPE-backed skeleton indexing consumer."""

import asyncio
from typing import TYPE_CHECKING, Any, cast

import pytest

from nexus.bricks.search.skeleton_pipe_consumer import SkeletonPipeConsumer
from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.contracts.metadata import DT_PIPE

if TYPE_CHECKING:
    from nexus.bricks.search.skeleton_indexer import SkeletonIndexer


class _FakeIndexer:
    async def index_file(self, *, path_id: str | None, virtual_path: str, zone_id: str) -> None:
        return None

    async def delete_file(self, *, path_id: str | None, virtual_path: str, zone_id: str) -> None:
        return None


def _indexer() -> "SkeletonIndexer":
    return cast("SkeletonIndexer", _FakeIndexer())


class _Kernel:
    def __init__(self, owner: "_FakeNx") -> None:
        self._owner = owner

    def destroy_pipe(self, path: str) -> None:
        self._owner.closed_paths.add(path)


class _FakeNx:
    def __init__(self) -> None:
        self.setattr_calls: list[tuple[str, dict[str, Any]]] = []
        self.write_calls: list[tuple[str, bytes, Any]] = []
        self.read_contexts: list[Any] = []
        self.closed_paths: set[str] = set()
        self._kernel = _Kernel(self)

    def sys_setattr(self, path: str, **attrs: Any) -> None:
        self.setattr_calls.append((path, attrs))

    def sys_read(self, path: str, *, context: Any | None = None) -> bytes:
        self.read_contexts.append(context)
        if path in self.closed_paths:
            raise NexusFileNotFoundError(path)
        return b""

    def sys_write(self, path: str, data: bytes, *, context: Any | None = None) -> dict[str, Any]:
        self.write_calls.append((path, data, context))
        return {"path": path, "bytes_written": len(data)}


@pytest.mark.asyncio
async def test_start_creates_vfs_dt_pipe_inode() -> None:
    nx = _FakeNx()
    consumer = SkeletonPipeConsumer(indexer=_indexer())
    consumer.bind_fs(nx)

    await consumer.start()
    try:
        assert nx.setattr_calls == [
            (
                "/nexus/pipes/skeleton-writes",
                {
                    "entry_type": DT_PIPE,
                    "capacity": 65_536,
                    "owner_id": "kernel",
                    "context": consumer._pipe_context,  # noqa: SLF001
                },
            )
        ]
        assert consumer._pipe_context.is_system  # noqa: SLF001
    finally:
        await consumer.stop()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_flush_uses_system_context_for_pipe_writes() -> None:
    nx = _FakeNx()
    consumer = SkeletonPipeConsumer(indexer=_indexer())
    consumer.bind_fs(nx)

    await consumer.start()
    try:
        consumer.notify_write("/docs/file.txt", "path-1", "root")

        for _ in range(20):
            if nx.write_calls:
                break
            await asyncio.sleep(0.01)

        assert len(nx.write_calls) == 1
        path, _data, context = nx.write_calls[0]
        assert path == "/nexus/pipes/skeleton-writes"
        assert context is consumer._pipe_context  # noqa: SLF001
        assert context.is_system
    finally:
        await consumer.stop()
        await asyncio.sleep(0)
