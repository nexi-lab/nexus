from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine, inspect, text

from nexus.storage.schema_invariants import (
    _ensure_file_paths_search_columns,
    _ensure_operation_log_snapshot_hash_text,
    _ensure_rebac_namespaces_table,
    _ensure_version_history_content_columns,
    _ensure_zone_indexes,
    _ensure_zones_table_shape,
    ensure_postgres_schema_invariants,
    ensure_zone_v1_schema_invariants,
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


class _FakeInspector:
    def __init__(self, columns_by_table: dict[str, list[dict[str, Any]]]) -> None:
        self._columns = columns_by_table

    def get_columns(self, table_name: str) -> list[dict[str, Any]]:
        return self._columns.get(table_name, [])


def test_ensure_operation_log_snapshot_hash_retypes_varchar_to_text() -> None:
    """#4645: legacy VARCHAR(64) snapshot_hash must widen to TEXT."""

    class _Varchar64:
        length = 64

    conn = RecordingConnection()
    inspector = _FakeInspector({"operation_log": [{"name": "snapshot_hash", "type": _Varchar64()}]})

    _ensure_operation_log_snapshot_hash_text(conn, inspector, {"operation_log"})

    statements = "\n".join(conn.statements)
    assert "ALTER COLUMN snapshot_hash TYPE TEXT" in statements


def test_ensure_operation_log_snapshot_hash_noop_when_already_text() -> None:
    class _Text:
        length = None

    conn = RecordingConnection()
    inspector = _FakeInspector({"operation_log": [{"name": "snapshot_hash", "type": _Text()}]})

    _ensure_operation_log_snapshot_hash_text(conn, inspector, {"operation_log"})

    assert conn.statements == []


def test_ensure_operation_log_snapshot_hash_repairs_nameonly_fake_columns() -> None:
    # Unit-test fakes exposing only column names are treated as legacy.
    conn = RecordingConnection()
    inspector = _FakeInspector({"operation_log": [{"name": "snapshot_hash"}]})

    _ensure_operation_log_snapshot_hash_text(conn, inspector, {"operation_log"})

    statements = "\n".join(conn.statements)
    assert "ALTER COLUMN snapshot_hash TYPE TEXT" in statements


def test_ensure_version_history_content_columns_repairs_legacy_content_hash() -> None:
    conn = RecordingConnection()
    columns_by_table = {"version_history": {"version_id", "content_hash"}}

    _ensure_version_history_content_columns(conn, columns_by_table)

    statements = "\n".join(conn.statements)
    assert "content_id" in columns_by_table["version_history"]
    assert "ADD COLUMN content_id VARCHAR(255)" in statements
    assert "SET content_id = content_hash" in statements
    assert "ALTER COLUMN content_hash DROP NOT NULL" in statements


def _legacy_relation_sources_engine(with_zone_grants: bool = True) -> Any:
    """SQLite engine carrying the pre-d4797ec230 schema (no zone_id column).

    Old deployments were built by create_all on the earlier model revision;
    rebuilding the legacy DDL directly is the only faithful simulation
    (DROP COLUMN cannot remove an indexed column on SQLite).
    """
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        if with_zone_grants:
            conn.execute(
                text(
                    """
                    CREATE TABLE zone_grants (
                        grant_id VARCHAR(64) PRIMARY KEY,
                        zone_id VARCHAR(255) NOT NULL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "INSERT INTO zone_grants (grant_id, zone_id) "
                    "VALUES ('g1', 'org-a'), ('g2', 'org-b')"
                )
            )
        conn.execute(
            text(
                """
                CREATE TABLE rebac_relation_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject VARCHAR(255) NOT NULL,
                    relation VARCHAR(128) NOT NULL,
                    object VARCHAR(255) NOT NULL,
                    source_grant_id VARCHAR(64),
                    reference_state VARCHAR(16) NOT NULL,
                    created_at TIMESTAMP,
                    updated_at TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO rebac_relation_sources "
                "(subject, relation, object, source_grant_id, reference_state) "
                "VALUES ('s1', 'r', '/', 'g1', 'ACTIVE'), "
                "('s2', 'r', '/', 'g2', 'ACTIVE')"
            )
        )
    return engine


def test_ensure_zone_v1_skips_when_table_missing() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)"))

    # No rebac_relation_sources → partial-schema environment stays non-fatal.
    ensure_zone_v1_schema_invariants(engine)


def test_ensure_zone_v1_upgrades_legacy_table_and_backfills() -> None:
    engine = _legacy_relation_sources_engine()

    ensure_zone_v1_schema_invariants(engine)

    inspector = inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("rebac_relation_sources")}
    assert "zone_id" in columns
    indexes = {i["name"] for i in inspector.get_indexes("rebac_relation_sources")}
    assert "ix_rebac_rel_src_zone" in indexes
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT source_grant_id, zone_id FROM rebac_relation_sources ORDER BY id")
        ).fetchall()
        assert rows == [("g1", "org-a"), ("g2", "org-b")]


def test_ensure_zone_v1_is_idempotent_after_upgrade() -> None:
    engine = _legacy_relation_sources_engine()

    ensure_zone_v1_schema_invariants(engine)
    ensure_zone_v1_schema_invariants(engine)  # second run is a no-op

    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM rebac_relation_sources WHERE zone_id IS NULL")
        ).scalar_one()
        assert count == 0


def test_ensure_zone_v1_fails_closed_on_authoritative_rows() -> None:
    engine = _legacy_relation_sources_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO rebac_relation_sources "
                "(subject, relation, object, source_grant_id, reference_state) "
                "VALUES ('s3', 'r', '/', NULL, 'ACTIVE')"
            )
        )

    with pytest.raises(RuntimeError, match="authoritative"):
        ensure_zone_v1_schema_invariants(engine)


def test_ensure_zone_v1_fails_closed_without_zone_grants() -> None:
    engine = _legacy_relation_sources_engine(with_zone_grants=False)

    with pytest.raises(RuntimeError, match="zone_grants"):
        ensure_zone_v1_schema_invariants(engine)
