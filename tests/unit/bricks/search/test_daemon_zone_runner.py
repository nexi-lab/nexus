from __future__ import annotations

import sys
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
