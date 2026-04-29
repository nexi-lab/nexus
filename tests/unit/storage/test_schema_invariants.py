"""Tests for schema invariant repair helpers."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import create_engine

from nexus.storage import schema_invariants
from nexus.storage.models._base import Base
from nexus.storage.schema_invariants import ensure_postgres_schema_invariants


def test_postgres_schema_invariants_noop_for_sqlite() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    ensure_postgres_schema_invariants(engine)


class _FakeDialect:
    name = "postgresql"


class _FakeConnection:
    def __init__(self, engine: "_FakePostgresEngine") -> None:
        self._engine = engine

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> None:
        sql = " ".join(str(statement).split())
        self._engine.executed.append((sql, params or {}))


class _FakeBegin:
    def __init__(self, engine: "_FakePostgresEngine") -> None:
        self._engine = engine

    def __enter__(self) -> _FakeConnection:
        return _FakeConnection(self._engine)

    def __exit__(self, *_exc: object) -> None:
        return None


class _FakePostgresEngine:
    dialect = _FakeDialect()

    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any]]] = []

    def begin(self) -> _FakeBegin:
        return _FakeBegin(self)


class _FakeInspector:
    def __init__(self, table_columns: dict[str, set[str]]) -> None:
        self._table_columns = table_columns

    def get_table_names(self) -> list[str]:
        return list(self._table_columns)

    def get_columns(self, table_name: str) -> list[dict[str, str]]:
        return [{"name": column} for column in self._table_columns[table_name]]


def test_postgres_schema_invariants_repair_zone_schema_gaps(monkeypatch) -> None:
    """Docker bootstrap must repair migration-only schemas before writes start."""
    engine = _FakePostgresEngine()
    inspector = _FakeInspector(
        {
            "metadata_change_log": {"sequence_number"},
            "file_paths": {
                "path_id",
                "virtual_path",
                "content_id",
                "indexed_content_id",
                "backend_id",
                "physical_path",
            },
            "version_history": {
                "version_id",
                "resource_type",
                "resource_id",
                "version_number",
                "content_id",
            },
            "rebac_changelog": {
                "change_id",
                "subject_id",
                "object_id",
                "created_at",
            },
            "rebac_tuples": {
                "tuple_id",
                "subject_id",
                "object_id",
                "zone_id",
                "subject_zone_id",
                "object_zone_id",
            },
            "rebac_group_closure": {
                "member_type",
                "member_id",
                "group_type",
                "group_id",
                "tenant_id",
                "depth",
            },
            "subscriptions": {"subscription_id", "tenant_id"},
            "tiger_cache": {
                "cache_id",
                "subject_type",
                "subject_id",
                "permission",
                "resource_type",
                "tenant_id",
            },
            "tiger_cache_queue": {
                "queue_id",
                "subject_type",
                "subject_id",
                "permission",
                "resource_type",
                "tenant_id",
            },
            "tiger_directory_grants": {"grant_id", "tenant_id", "directory_path"},
        }
    )
    monkeypatch.setattr(schema_invariants, "inspect", lambda _engine: inspector)

    ensure_postgres_schema_invariants(cast(Any, engine))

    executed_sql = "\n".join(sql for sql, _params in engine.executed)
    assert "ALTER TABLE file_paths ADD COLUMN zone_id VARCHAR(255) NOT NULL DEFAULT 'root'" in (
        executed_sql
    )
    assert (
        "ALTER TABLE rebac_changelog ADD COLUMN zone_id VARCHAR(255) NOT NULL DEFAULT 'root'"
        in executed_sql
    )
    assert (
        "ALTER TABLE tiger_directory_grants ADD COLUMN zone_id VARCHAR(255) NOT NULL DEFAULT 'root'"
        in executed_sql
    )
    assert (
        "ALTER TABLE subscriptions ADD COLUMN zone_id VARCHAR(36) NOT NULL DEFAULT 'root'"
        in executed_sql
    )
    assert "ALTER TABLE rebac_tuples ALTER COLUMN subject_id TYPE VARCHAR(255)" in executed_sql
    assert "ALTER TABLE rebac_tuples ALTER COLUMN object_id TYPE VARCHAR(255)" in executed_sql
    assert "ALTER TABLE rebac_changelog ALTER COLUMN subject_id TYPE VARCHAR(255)" in executed_sql
    assert "ALTER TABLE rebac_changelog ALTER COLUMN object_id TYPE VARCHAR(255)" in executed_sql
    assert "ALTER TABLE file_paths ALTER COLUMN content_id TYPE VARCHAR(255)" in executed_sql
    assert (
        "ALTER TABLE file_paths ALTER COLUMN indexed_content_id TYPE VARCHAR(255)" in executed_sql
    )
    assert "ALTER TABLE version_history ALTER COLUMN content_id TYPE VARCHAR(255)" in executed_sql
    assert "ALTER TABLE file_paths ALTER COLUMN backend_id DROP NOT NULL" in executed_sql
    assert "ALTER TABLE file_paths ALTER COLUMN physical_path DROP NOT NULL" in executed_sql
    assert "ALTER TABLE tiger_cache ADD COLUMN zone_id VARCHAR(255) NOT NULL DEFAULT 'root'" in (
        executed_sql
    )
    assert (
        "ALTER TABLE tiger_cache_queue ADD COLUMN zone_id VARCHAR(255) NOT NULL DEFAULT 'root'"
        in executed_sql
    )
    assert (
        "ALTER TABLE rebac_group_closure ADD COLUMN zone_id VARCHAR(255) NOT NULL DEFAULT 'root'"
        in executed_sql
    )
    assert "ALTER TABLE tiger_cache ALTER COLUMN tenant_id SET DEFAULT 'root'" in executed_sql
    assert "ALTER TABLE tiger_cache_queue ALTER COLUMN tenant_id SET DEFAULT 'root'" in executed_sql
    assert (
        "ALTER TABLE rebac_group_closure ALTER COLUMN tenant_id SET DEFAULT 'root'" in executed_sql
    )
    assert "DROP CONSTRAINT uq_tiger_cache" in executed_sql
    assert "ADD CONSTRAINT uq_tiger_cache UNIQUE" in executed_sql
    assert "DROP CONSTRAINT IF EXISTS rebac_group_closure_pkey" in executed_sql
    assert "ADD PRIMARY KEY (member_type, member_id, group_type, group_id, zone_id)" in executed_sql
    assert (
        "ALTER TABLE tiger_directory_grants ALTER COLUMN tenant_id SET DEFAULT 'root'"
        in executed_sql
    )
