from __future__ import annotations

import asyncio
import sys
import threading
import types
from typing import Any

import pytest


class _RuntimeStub(types.ModuleType):
    def __getattr__(self, name: str) -> Any:
        def _missing(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError(f"nexus_runtime stub called: {name}")

        return _missing


sys.modules.setdefault("nexus_runtime", _RuntimeStub("nexus_runtime"))


class RaisingRegistry:
    def runner_for(self, zone_id: str) -> Any:
        raise AssertionError(f"scope mutation unexpectedly entered zone runner {zone_id}")


@pytest.mark.asyncio
async def test_scope_crud_mutation_stays_on_current_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.bricks.search import scope_ops
    from nexus.bricks.search.daemon import SearchDaemon

    daemon = SearchDaemon.__new__(SearchDaemon)
    daemon._zone_registry = RaisingRegistry()
    calls: list[tuple[Any, str, str]] = []

    async def fake_add_indexed_directory(
        daemon_arg: Any,
        zone_id: str,
        directory_path: str,
    ) -> tuple[str, str]:
        calls.append((daemon_arg, zone_id, directory_path))
        return directory_path, "ok"

    monkeypatch.setattr(scope_ops, "add_indexed_directory", fake_add_indexed_directory)

    result = await daemon.add_indexed_directory("eng", "/docs")

    assert result == ("/docs", "ok")
    assert calls == [(daemon, "eng", "/docs")]


@pytest.mark.asyncio
async def test_search_with_zone_stays_on_daemon_owner_loop() -> None:
    from nexus.bricks.search.daemon import SearchDaemon
    from nexus.runtime.zone_runner import ZoneRegistry

    daemon = SearchDaemon.__new__(SearchDaemon)
    daemon._owner_loop = asyncio.get_running_loop()
    daemon._zone_registry = ZoneRegistry()
    seen: list[tuple[asyncio.AbstractEventLoop, int, str | None]] = []

    async def fake_search_on_current_loop(
        query: str,
        *,
        search_type: str,
        limit: int,
        path_filter: str | None,
        alpha: float,
        fusion_method: str,
        zone_id: str | None,
    ) -> list[str]:
        seen.append((asyncio.get_running_loop(), threading.get_ident(), zone_id))
        return [query, search_type, str(limit), str(path_filter), str(alpha), fusion_method]

    daemon._search_on_current_loop = fake_search_on_current_loop

    try:
        result = await daemon.search("sku", zone_id="default")
    finally:
        daemon._zone_registry.stop_all()

    assert result == ["sku", "hybrid", "10", "None", "0.5", "rrf"]
    assert seen == [(daemon._owner_loop, threading.get_ident(), "default")]
