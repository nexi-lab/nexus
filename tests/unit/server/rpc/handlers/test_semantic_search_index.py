from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from nexus.server.rpc.handlers.filesystem import handle_semantic_search_index


class _Rows:
    def __init__(self, rows: list[tuple[str, str | None]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[str, str | None]]:
        return self._rows

    def fetchone(self) -> tuple[str | None] | None:
        if not self._rows:
            return None
        return (self._rows[0][1],)


class _Session:
    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, _statement: Any, _params: dict[str, Any]) -> _Rows:
        return _Rows([("/workspace/demo/herb/products/prod-001.md", "hash-1")])


class _Daemon:
    def __init__(self) -> None:
        self._indexing_pipeline = object()
        self._async_session = _Session
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self.indexed: list[tuple[list[dict[str, Any]], str | None]] = []
        self.index_loops: list[asyncio.AbstractEventLoop] = []

    async def index_documents(
        self, documents: list[dict[str, Any]], *, zone_id: str | None = None
    ) -> int:
        self.index_loops.append(asyncio.get_running_loop())
        self.indexed.append((documents, zone_id))
        return len(documents)

    async def delete_documents(self, _ids: list[str], *, zone_id: str | None = None) -> int:
        return 0

    async def _run_on_owner_loop(self, work: Any) -> Any:
        owner_loop = self._owner_loop
        current_loop = asyncio.get_running_loop()
        if owner_loop is None or owner_loop is current_loop:
            return await work()
        submitted = asyncio.run_coroutine_threadsafe(work(), owner_loop)
        return await asyncio.wrap_future(submitted)


class _SearchService:
    def __init__(self, daemon: _Daemon) -> None:
        self._search_daemon = daemon

    async def ainitialize_semantic_search(self, **_kwargs: Any) -> None:
        raise AssertionError("current daemon indexing pipeline should be used")


class _NexusFs:
    def __init__(self, search: _SearchService) -> None:
        self._search = search

    def service(self, name: str) -> _SearchService | None:
        return self._search if name == "search" else None

    def sys_read(self, _path: str, *, context: Any) -> bytes:
        assert context.zone_id == "root"
        return b"Nexus Core SKU catalog details"


@pytest.mark.asyncio
async def test_semantic_search_index_uses_daemon_pipeline_without_legacy_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nexus.factory._semantic_search._resolve_parse_fn", lambda _nx: None)
    monkeypatch.setattr(
        "nexus.factory.adapters._apply_parse_transform_with_status",
        lambda _nx, _path, raw, *, parse_fn, content_id: (raw.decode(), "plain"),
    )
    monkeypatch.setitem(sys.modules, "sqlalchemy", SimpleNamespace(text=lambda sql: sql))

    daemon = _Daemon()
    result = await handle_semantic_search_index(
        _NexusFs(_SearchService(daemon)),
        SimpleNamespace(path="/workspace/demo", recursive=True),
        SimpleNamespace(zone_id="root"),
    )

    assert result["total_files"] == 1
    assert result["total_chunks"] == 1
    assert daemon.indexed == [
        (
            [
                {
                    "id": "/workspace/demo/herb/products/prod-001.md",
                    "text": "Nexus Core SKU catalog details",
                    "path": "/workspace/demo/herb/products/prod-001.md",
                }
            ],
            "root",
        )
    ]


@pytest.mark.asyncio
async def test_semantic_search_index_runs_daemon_work_on_owner_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.runtime.zone_runner import ZoneRunner

    monkeypatch.setattr("nexus.factory._semantic_search._resolve_parse_fn", lambda _nx: None)
    monkeypatch.setattr(
        "nexus.factory.adapters._apply_parse_transform_with_status",
        lambda _nx, _path, raw, *, parse_fn, content_id: (raw.decode(), "plain"),
    )
    monkeypatch.setitem(sys.modules, "sqlalchemy", SimpleNamespace(text=lambda sql: sql))

    owner_loop = asyncio.get_running_loop()
    daemon = _Daemon()
    daemon._owner_loop = owner_loop
    runner = ZoneRunner("root")

    try:
        result = await runner.call(
            lambda: handle_semantic_search_index(
                _NexusFs(_SearchService(daemon)),
                SimpleNamespace(path="/workspace/demo", recursive=True),
                SimpleNamespace(zone_id="root"),
            )
        )
    finally:
        runner.stop()

    assert result["total_files"] == 1
    assert daemon.index_loops == [owner_loop]
