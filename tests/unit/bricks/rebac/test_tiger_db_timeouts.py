"""Regression tests for Tiger write-through DB timeouts (Issue #4359)."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pyroaring")

from nexus.bricks.rebac.cache.tiger.bitmap_cache import TigerCache
from nexus.bricks.rebac.cache.tiger.resource_map import TigerResourceMap


class _Row:
    resource_int_id = 7


class _Result:
    def __init__(self, row: Any = None) -> None:
        self._row = row

    def first(self) -> Any:
        return self._row

    def fetchone(self) -> Any:
        return self._row


class _Connection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.commits = 0

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> _Result:
        sql = str(statement)
        self.statements.append(sql)
        if sql.startswith("INSERT INTO tiger_resource_map"):
            return _Result(_Row())
        return _Result()

    def commit(self) -> None:
        self.commits += 1


class _Engine:
    url = "postgresql://example/nexus"

    def __init__(self) -> None:
        self.connection = _Connection()

    def connect(self) -> _Connection:
        return self.connection

    def begin(self) -> _Connection:
        return self.connection


class _ResourceMap:
    def get_or_create_int_id(self, _resource_type: str, _resource_id: str) -> int:
        return 7


def test_resource_map_sets_local_timeouts_before_postgres_write(monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_TIGER_WRITE_TIMEOUT_SECONDS", "0.25")
    engine = _Engine()
    resource_map = TigerResourceMap(engine, is_postgresql=True)

    resource_int_id = resource_map.get_or_create_int_id("file", "/boot.txt")

    assert resource_int_id == 7
    assert engine.connection.statements[:2] == [
        "SET LOCAL lock_timeout = '250ms'",
        "SET LOCAL statement_timeout = '250ms'",
    ]


def test_resource_map_treats_empty_timeout_env_as_default(monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_TIGER_WRITE_TIMEOUT_SECONDS", "")
    engine = _Engine()
    resource_map = TigerResourceMap(engine, is_postgresql=True)

    resource_int_id = resource_map.get_or_create_int_id("file", "/boot.txt")

    assert resource_int_id == 7
    assert engine.connection.statements[:2] == [
        "SET LOCAL lock_timeout = '5000ms'",
        "SET LOCAL statement_timeout = '5000ms'",
    ]


def test_persist_single_grant_sets_local_timeouts_before_tiger_cache_write(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NEXUS_TIGER_WRITE_TIMEOUT_SECONDS", "0.25")
    engine = _Engine()
    cache = TigerCache(
        engine=engine,
        resource_map=_ResourceMap(),
        is_postgresql=True,
    )

    result = cache.persist_single_grant(
        subject_type="user",
        subject_id="alice",
        permission="read",
        resource_type="file",
        resource_id="/boot.txt",
        zone_id="root",
    )

    assert result is True
    assert engine.connection.statements[:2] == [
        "SET LOCAL lock_timeout = '250ms'",
        "SET LOCAL statement_timeout = '250ms'",
    ]
    assert any(
        statement.startswith("INSERT INTO tiger_cache")
        for statement in engine.connection.statements
    )
