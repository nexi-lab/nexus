from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine, inspect

from nexus.storage.schema_invariants import (
    _ensure_file_paths_search_columns,
    _ensure_rebac_namespaces_table,
    _ensure_version_history_content_columns,
    _ensure_zone_indexes,
    _ensure_zones_table_shape,
    ensure_postgres_schema_invariants,
)


class RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))


def test_ensure_zones_table_shape_repairs_legacy_columns() -> None:
    conn = RecordingConnection()
    columns_by_table = {
        "zones": {"zone_id", "name", "domain", "description", "settings"},
    }

    _ensure_zones_table_shape(conn, columns_by_table)

    statements = "\n".join(conn.statements)
    for column in (
        "indexing_mode",
        "phase",
        "finalizers",
        "deleted_at",
        "created_at",
        "updated_at",
    ):
        assert column in columns_by_table["zones"]
        assert f"ADD COLUMN {column}" in statements

    assert "ALTER COLUMN indexing_mode SET NOT NULL" in statements
    assert "ALTER COLUMN phase SET NOT NULL" in statements
    assert "ALTER COLUMN finalizers SET NOT NULL" in statements


def test_ensure_zone_indexes_covers_zones_table() -> None:
    conn = RecordingConnection()
    columns_by_table = {
        "zones": {"zone_id", "name", "phase", "deleted_at"},
    }

    _ensure_zone_indexes(conn, columns_by_table)

    statements = "\n".join(conn.statements)
    assert "idx_zones_name" in statements
    assert "idx_zones_phase" in statements
    assert "ix_zones_deleted_at" in statements


def test_ensure_file_paths_search_columns_repairs_legacy_table() -> None:
    conn = RecordingConnection()
    columns_by_table = {"file_paths": {"path_id", "content_hash", "indexed_content_hash"}}

    _ensure_file_paths_search_columns(conn, columns_by_table)

    statements = "\n".join(conn.statements)
    assert "content_id" in columns_by_table["file_paths"]
    assert "indexed_content_id" in columns_by_table["file_paths"]
    assert "last_indexed_at" in columns_by_table["file_paths"]
    assert "ADD COLUMN content_id VARCHAR(255)" in statements
    assert "SET content_id = content_hash" in statements
    assert "ADD COLUMN indexed_content_id VARCHAR(255)" in statements
    assert "SET indexed_content_id = indexed_content_hash" in statements
    assert "ADD COLUMN last_indexed_at TIMESTAMP" in statements


def test_ensure_rebac_namespaces_table_creates_missing_table() -> None:
    conn = RecordingConnection()
    columns_by_table: dict[str, set[str]] = {}
    table_names: set[str] = set()

    _ensure_rebac_namespaces_table(conn, columns_by_table, table_names)

    statements = "\n".join(conn.statements)
    assert "rebac_namespaces" in table_names
    assert "rebac_namespaces" in columns_by_table
    assert "CREATE TABLE IF NOT EXISTS rebac_namespaces" in statements


def test_schema_invariants_create_rebac_namespaces_for_sqlite() -> None:
    engine = create_engine("sqlite:///:memory:")

    ensure_postgres_schema_invariants(engine)

    assert "rebac_namespaces" in inspect(engine).get_table_names()


def test_ensure_version_history_content_columns_repairs_legacy_content_hash() -> None:
    conn = RecordingConnection()
    columns_by_table = {"version_history": {"version_id", "content_hash"}}

    _ensure_version_history_content_columns(conn, columns_by_table)

    statements = "\n".join(conn.statements)
    assert "content_id" in columns_by_table["version_history"]
    assert "ADD COLUMN content_id VARCHAR(255)" in statements
    assert "SET content_id = content_hash" in statements
    assert "ALTER COLUMN content_hash DROP NOT NULL" in statements
