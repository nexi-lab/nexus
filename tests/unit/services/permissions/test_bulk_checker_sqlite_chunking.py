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
