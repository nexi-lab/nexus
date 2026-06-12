"""Regression tests for SQLite bulk ReBAC query chunking."""

from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine

from nexus.bricks.rebac.batch.bulk_checker import BulkPermissionChecker


class _RecordingConnection:
    def __init__(self, max_params: int) -> None:
        self.max_params = max_params
        self.param_counts: list[int] = []

    def execute(self, _stmt: Any, params: dict[str, Any]) -> list[Any]:
        param_count = len(params)
        self.param_counts.append(param_count)
        assert param_count <= self.max_params
        return []


class _FakeResourceMap:
    def __init__(self) -> None:
        self._ids: dict[tuple[str, str], int] = {}

    def get_int_ids_batch(self, resource_keys: list[tuple[str, str]]) -> dict[tuple[str, str], int]:
        return {key: self._ids[key] for key in resource_keys if key in self._ids}

    def get_or_create_int_id(self, resource_type: str, resource_id: str) -> int:
        key = (resource_type, resource_id)
        if key not in self._ids:
            self._ids[key] = len(self._ids) + 1
        return self._ids[key]


class _FakeTigerCache:
    def __init__(self) -> None:
        self._resource_map = _FakeResourceMap()
        self.bulk_adds: list[set[int]] = []
        self.persists: list[set[int]] = []

    def add_to_bitmap_bulk(
        self,
        *,
        resource_int_ids: set[int],
        **_kwargs: object,
    ) -> None:
        self.bulk_adds.append(set(resource_int_ids))

    def persist_bitmap_bulk(
        self,
        *,
        resource_int_ids: set[int],
        **_kwargs: object,
    ) -> None:
        self.persists.append(set(resource_int_ids))


def _make_checker() -> BulkPermissionChecker:
    return BulkPermissionChecker(
        engine=create_engine("sqlite:///:memory:"),
        get_namespace=lambda _entity_type: None,
        enforce_zone_isolation=True,
        l1_cache=None,
        tiger_cache=None,
        compute_bulk_helper=lambda *_args, **_kwargs: False,
        rebac_check_single=lambda *_args, **_kwargs: False,
        cache_result=lambda *_args, **_kwargs: None,
        tuple_version=0,
    )


def test_sqlite_bulk_tuple_fetch_chunks_large_entity_lists() -> None:
    checker = _make_checker()
    conn = _RecordingConnection(max_params=999)
    entities = [("file", f"/doc-{i}.txt") for i in range(600)]

    rows = checker._fetch_all_tuples_single_query(
        conn,
        entities,
        zone_id="zone-a",
        now_iso="2026-04-26T00:00:00+00:00",
    )

    assert rows == []
    assert len(conn.param_counts) > 1


def test_sqlite_cross_zone_tuple_fetch_chunks_large_subject_lists() -> None:
    checker = _make_checker()
    conn = _RecordingConnection(max_params=999)
    tuples_graph: list[dict[str, Any]] = []
    subjects = [("user", f"user-{i}") for i in range(600)]

    count = checker._fetch_cross_zone_tuples(
        conn,
        subjects,
        tuples_graph,
        now_iso="2026-04-26T00:00:00+00:00",
    )

    assert count == 0
    assert tuples_graph == []
    assert len(conn.param_counts) > 1


def test_tiger_write_through_skips_results_from_path_pattern_tuples() -> None:
    tiger_cache = _FakeTigerCache()
    checker = BulkPermissionChecker(
        engine=create_engine("sqlite:///:memory:"),
        get_namespace=lambda _entity_type: None,
        enforce_zone_isolation=False,
        l1_cache=None,
        tiger_cache=tiger_cache,
        compute_bulk_helper=lambda *_args, **_kwargs: True,
        rebac_check_single=lambda *_args, **_kwargs: False,
        cache_result=lambda *_args, **_kwargs: None,
        tuple_version=0,
    )

    check = (("user", "admin"), "read", ("file", "/workspaces/a.md"))
    checker._phase_python_compute(
        [check],
        [
            {
                "subject_type": "user",
                "subject_id": "admin",
                "subject_relation": None,
                "relation": "read",
                "object_type": "file",
                "object_id": "/workspaces/**",
            }
        ],
        "root",
        {},
        {},
        {"hits": 0, "misses": 0, "max_depth": 0},
    )

    assert tiger_cache.bulk_adds == []
    assert tiger_cache.persists == []
