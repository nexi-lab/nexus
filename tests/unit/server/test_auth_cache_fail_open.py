"""Auth-result cache is fail-open and optionally observable (#4777 follow-up).

A cache backend error must read as a miss (the provider is re-asked), never
as a 500; a set failure leaves auth uncached.  With NEXUS_AUTH_CACHE_DEBUG
each get/set outcome is logged so a live 100 % miss rate can be diagnosed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from nexus.server import dependencies as deps


class _BrokenStore:
    def __init__(self, *, get_error: bool = False, set_error: bool = False) -> None:
        self.get_error = get_error
        self.set_error = set_error
        self.data: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        if self.get_error:
            raise ConnectionError("dragonfly down")
        return self.data.get(key)

    async def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        if self.set_error:
            raise ConnectionError("dragonfly read-only")
        self.data[key] = value


def test_get_error_reads_as_miss(caplog: pytest.LogCaptureFixture) -> None:
    store: Any = _BrokenStore(get_error=True)
    with caplog.at_level(logging.WARNING, logger=deps.logger.name):
        assert asyncio.run(deps._get_cached_auth(store, "sk-x")) is None
    assert any("[AUTH-CACHE] get failed" in r.getMessage() for r in caplog.records)


def test_set_error_is_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    store: Any = _BrokenStore(set_error=True)
    with caplog.at_level(logging.WARNING, logger=deps.logger.name):
        asyncio.run(deps._set_cached_auth(store, "sk-x", {"authenticated": True}))
    assert any("[AUTH-CACHE] set failed" in r.getMessage() for r in caplog.records)
    assert store.data == {}


def test_round_trip_and_debug_lines(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(deps, "_AUTH_CACHE_DEBUG", True)
    store: Any = _BrokenStore()
    entry: dict[str, Any] = {"authenticated": True, "subject_id": "alice"}
    with caplog.at_level(logging.INFO, logger=deps.logger.name):
        asyncio.run(deps._set_cached_auth(store, "sk-x", entry))
        got = asyncio.run(deps._get_cached_auth(store, "sk-x"))
    assert got == entry
    assert json.loads(next(iter(store.data.values()))) == entry
    msgs = [r.getMessage() for r in caplog.records]
    assert any("[AUTH-CACHE] set ok" in m for m in msgs)
    assert any("[AUTH-CACHE] get hit" in m for m in msgs)


def test_debug_off_logs_nothing_on_success(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(deps, "_AUTH_CACHE_DEBUG", False)
    store: Any = _BrokenStore()
    with caplog.at_level(logging.INFO, logger=deps.logger.name):
        asyncio.run(deps._set_cached_auth(store, "sk-x", {"authenticated": True}))
        asyncio.run(deps._get_cached_auth(store, "sk-x"))
    assert not [r for r in caplog.records if "[AUTH-CACHE]" in r.getMessage()]
