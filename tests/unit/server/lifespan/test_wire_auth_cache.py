"""Lifespan wiring of ``app.state.auth_cache_store`` (#4777).

``dependencies.get_auth_result`` has always consulted this attribute, but no
startup code assigned it — every request re-authenticated against the
provider (100 % ``(cache miss)`` in ``[AUTH-TIMING]``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from nexus.contracts.cache_store import InMemoryCacheStore, NullCacheStore
from nexus.server.lifespan import _size_default_executor, _wire_auth_cache


def _app(**state: Any) -> Any:
    """A stand-in for FastAPI: only ``app.state`` is consulted."""
    return SimpleNamespace(state=SimpleNamespace(**state))


def test_no_auth_provider_leaves_cache_unset() -> None:
    app = _app(auth_provider=None, auth_cache_store=None, cache_brick=None)
    _wire_auth_cache(app)
    assert app.state.auth_cache_store is None


def test_falls_back_to_process_local_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_AUTH_CACHE_MAX_ENTRIES", "7")
    app = _app(auth_provider=object(), auth_cache_store=None, cache_brick=None)
    _wire_auth_cache(app)
    store = app.state.auth_cache_store
    assert isinstance(store, InMemoryCacheStore)
    assert store._max_size == 7


def test_null_cache_brick_store_is_not_reused() -> None:
    brick = SimpleNamespace(has_cache_store=False, cache_store=NullCacheStore())
    app = _app(auth_provider=object(), auth_cache_store=None, cache_brick=brick)
    _wire_auth_cache(app)
    assert isinstance(app.state.auth_cache_store, InMemoryCacheStore)


def test_shared_cache_brick_store_is_reused() -> None:
    shared = InMemoryCacheStore()
    brick = SimpleNamespace(has_cache_store=True, cache_store=shared)
    app = _app(auth_provider=object(), auth_cache_store=None, cache_brick=brick)
    _wire_auth_cache(app)
    assert app.state.auth_cache_store is shared


def test_preconfigured_store_is_kept() -> None:
    preset = InMemoryCacheStore()
    app = _app(auth_provider=object(), auth_cache_store=preset, cache_brick=None)
    _wire_auth_cache(app)
    assert app.state.auth_cache_store is preset


def test_wired_store_round_trips_through_dependencies_helpers() -> None:
    from nexus.server.dependencies import _get_cached_auth, _set_cached_auth

    app = _app(auth_provider=object(), auth_cache_store=None, cache_brick=None)
    _wire_auth_cache(app)
    store = app.state.auth_cache_store

    async def _roundtrip() -> dict[str, Any] | None:
        await _set_cached_auth(store, "sk-token", {"authenticated": True, "subject_id": "a"})
        return await _get_cached_auth(store, "sk-token")

    assert asyncio.run(_roundtrip()) == {"authenticated": True, "subject_id": "a"}


def test_default_executor_is_resized_to_thread_pool() -> None:
    async def _probe() -> int:
        _size_default_executor(17)
        loop = asyncio.get_running_loop()
        executor = getattr(loop, "_default_executor", None)
        assert executor is not None
        return int(executor._max_workers)

    assert asyncio.run(_probe()) == 17
