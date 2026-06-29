from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, String, create_engine, inspect, text
from sqlalchemy.engine.interfaces import ReflectedColumn


def _load_revision() -> ModuleType:
    revision_path = (
        Path(__file__).resolve().parents[2] / "alembic" / "versions" / "align_graph_zone_columns.py"
    )
    spec = importlib.util.spec_from_file_location("align_graph_zone_columns", revision_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REVISION = _load_revision()


def _run_revision(conn: Connection, direction: str = "upgrade") -> None:
    context = MigrationContext.configure(conn)
    with Operations.context(context):
        getattr(REVISION, direction)()


def _column_map(conn: Connection, table_name: str) -> dict[str, ReflectedColumn]:
    return {column["name"]: column for column in inspect(conn).get_columns(table_name)}


def _create_rebac_sentinel(conn: Connection) -> None:
    conn.execute(
        text(
            "CREATE TABLE rebac_tuples ("
            "tuple_id VARCHAR(36) PRIMARY KEY, zone_id VARCHAR(255) NOT NULL)"
        )
    )
    conn.execute(
        text("INSERT INTO rebac_tuples (tuple_id, zone_id) VALUES ('tuple-1', 'control-zone')")
    )


def test_upgrade_executes_revision_and_preserves_legacy_graph_data(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        conn.execute(
            text(
                "CREATE TABLE entities ("
                "entity_id VARCHAR(36) PRIMARY KEY, "
                "tenant_id VARCHAR(255) NOT NULL, canonical_name VARCHAR(512) NOT NULL)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE relationships ("
                "relationship_id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(255) NOT NULL, "
                "source_entity_id VARCHAR(36) NOT NULL "
                "REFERENCES entities(entity_id) ON DELETE CASCADE, "
                "target_entity_id VARCHAR(36) NOT NULL "
                "REFERENCES entities(entity_id) ON DELETE CASCADE)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE entity_mentions ("
                "mention_id VARCHAR(36) PRIMARY KEY, entity_id VARCHAR(36) NOT NULL "
                "REFERENCES entities(entity_id) ON DELETE CASCADE)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO entities (entity_id, tenant_id, canonical_name) "
                "VALUES ('e1', 'legacy-zone', 'Legacy Entity'), "
                "('e2', 'legacy-zone', 'Second Entity')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO relationships "
                "(relationship_id, tenant_id, source_entity_id, target_entity_id) "
                "VALUES ('r1', 'legacy-zone', 'e1', 'e2')"
            )
        )
        conn.execute(
            text("INSERT INTO entity_mentions (mention_id, entity_id) VALUES ('m1', 'e1')")
        )
        _create_rebac_sentinel(conn)

        _run_revision(conn)

        entity_columns = _column_map(conn, "entities")
        relationship_columns = _column_map(conn, "relationships")
        assert "tenant_id" not in entity_columns
        assert "tenant_id" not in relationship_columns
        # SQLite does not enforce VARCHAR lengths. Keeping the original type lets
        # the revision use its native, FK-safe column rename instead of rebuilding
        # a referenced parent table merely to change 255 to 64.
        assert getattr(entity_columns["zone_id"]["type"], "length", None) == 255
        assert getattr(relationship_columns["zone_id"]["type"], "length", None) == 255
        assert [
            tuple(row)
            for row in conn.execute(
                text("SELECT entity_id, zone_id FROM entities ORDER BY entity_id")
            )
        ] == [("e1", "legacy-zone"), ("e2", "legacy-zone")]
        assert conn.execute(text("SELECT zone_id FROM relationships")).scalar_one() == "legacy-zone"
        assert conn.execute(
            text("SELECT relationship_id, source_entity_id, target_entity_id FROM relationships")
        ).one() == ("r1", "e1", "e2")
        assert conn.execute(text("SELECT mention_id, entity_id FROM entity_mentions")).one() == (
            "m1",
            "e1",
        )
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert conn.execute(text("SELECT * FROM rebac_tuples")).one() == (
            "tuple-1",
            "control-zone",
        )


def test_upgrade_is_idempotent_for_already_aligned_tables(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'aligned.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE entities ("
                "entity_id VARCHAR(36) PRIMARY KEY, "
                "zone_id VARCHAR(255) NOT NULL, canonical_name VARCHAR(512) NOT NULL)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE relationships ("
                "relationship_id VARCHAR(36) PRIMARY KEY, zone_id VARCHAR(255) NOT NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO entities (entity_id, zone_id, canonical_name) "
                "VALUES ('e1', 'aligned-zone', 'Aligned Entity')"
            )
        )
        _create_rebac_sentinel(conn)

        _run_revision(conn)
        _run_revision(conn)

        assert getattr(_column_map(conn, "entities")["zone_id"]["type"], "length", None) == 255
        assert getattr(_column_map(conn, "relationships")["zone_id"]["type"], "length", None) == 255
        assert conn.execute(text("SELECT zone_id FROM entities")).scalar_one() == "aligned-zone"
        assert conn.execute(text("SELECT * FROM rebac_tuples")).one() == (
            "tuple-1",
            "control-zone",
        )


def test_upgrade_merges_partially_aligned_columns_before_dropping_legacy(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'partial.db'}")
    with engine.begin() as conn:
        for table_name, id_column in (
            ("entities", "entity_id"),
            ("relationships", "relationship_id"),
        ):
            conn.execute(
                text(
                    f"CREATE TABLE {table_name} ("
                    f"{id_column} VARCHAR(36) PRIMARY KEY, "
                    "tenant_id VARCHAR(64), zone_id VARCHAR(255))"
                )
            )
            conn.execute(
                text(
                    f"INSERT INTO {table_name} ({id_column}, tenant_id, zone_id) VALUES "
                    "('null-zone', 'legacy-null', NULL), "
                    "('empty-zone', 'legacy-empty', ''), "
                    "('kept-zone', 'already-aligned', 'already-aligned')"
                )
            )
        _create_rebac_sentinel(conn)

        _run_revision(conn)

        for table_name in ("entities", "relationships"):
            columns = _column_map(conn, table_name)
            assert "tenant_id" not in columns
            assert getattr(columns["zone_id"]["type"], "length", None) == 255
            assert conn.execute(
                text(f"SELECT zone_id FROM {table_name} ORDER BY rowid")
            ).scalars().all() == ["legacy-null", "legacy-empty", "already-aligned"]
        assert conn.execute(text("SELECT * FROM rebac_tuples")).one() == (
            "tuple-1",
            "control-zone",
        )


def test_upgrade_fails_closed_for_unsafe_sqlite_mixed_unique_constraints(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'partial-dependencies.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE entities ("
                "entity_id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(64), "
                "zone_id VARCHAR(64), canonical_name VARCHAR(512) NOT NULL, "
                "entity_type VARCHAR(64), "
                "CONSTRAINT uq_entity_tenant_name UNIQUE (tenant_id, canonical_name))"
            )
        )
        conn.execute(text("CREATE INDEX idx_entities_tenant ON entities (tenant_id)"))
        conn.execute(
            text("CREATE INDEX idx_entities_tenant_type ON entities (tenant_id, entity_type)")
        )
        conn.execute(
            text(
                "CREATE TABLE relationships ("
                "relationship_id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(64), "
                "zone_id VARCHAR(64), source_entity_id VARCHAR(36) NOT NULL, "
                "target_entity_id VARCHAR(36) NOT NULL, relationship_type VARCHAR(64) NOT NULL, "
                "CONSTRAINT uq_relationship_tuple UNIQUE ("
                "tenant_id, source_entity_id, target_entity_id, relationship_type))"
            )
        )
        conn.execute(text("CREATE INDEX idx_relationships_tenant ON relationships (tenant_id)"))

        with pytest.raises(
            RuntimeError,
            match="cannot safely remove tenant_id.*entities.*SQLite.*unique constraint",
        ):
            _run_revision(conn)

        assert set(_column_map(conn, "entities")) >= {"tenant_id", "zone_id"}
        assert set(_column_map(conn, "relationships")) >= {"tenant_id", "zone_id"}
        assert "idx_entities_tenant" in {
            index["name"] for index in inspect(conn).get_indexes("entities")
        }
        assert "uq_entity_tenant_name" in {
            constraint["name"] for constraint in inspect(conn).get_unique_constraints("entities")
        }


def test_upgrade_rejects_conflicting_mixed_rows_before_modifying_any_table(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'conflicting.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE entities ("
                "entity_id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(64) NOT NULL)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE relationships ("
                "relationship_id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(64), "
                "zone_id VARCHAR(64))"
            )
        )
        conn.execute(text("INSERT INTO entities VALUES ('e1', 'legacy-zone')"))
        conn.execute(
            text("INSERT INTO relationships VALUES ('r1', 'legacy-zone', 'different-zone')")
        )

        with pytest.raises(
            RuntimeError,
            match=(
                "Conflicting tenant_id and zone_id values in relationships; "
                "refusing to modify graph data"
            ),
        ):
            _run_revision(conn)

        assert set(_column_map(conn, "entities")) >= {"tenant_id"}
        assert "zone_id" not in _column_map(conn, "entities")
        assert set(_column_map(conn, "relationships")) >= {"tenant_id", "zone_id"}
        assert conn.execute(text("SELECT * FROM entities")).one() == ("e1", "legacy-zone")
        assert conn.execute(text("SELECT * FROM relationships")).one() == (
            "r1",
            "legacy-zone",
            "different-zone",
        )


def test_postgresql_mixed_reflection_filters_constraint_indexes_and_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspector = Mock()
    inspector.get_indexes.return_value = [
        {
            "name": "uq_entity_tenant_name",
            "column_names": ["tenant_id", "canonical_name"],
            "unique": True,
            "duplicates_constraint": "uq_entity_tenant_name",
        },
        {"name": "idx_entities_tenant", "column_names": ["tenant_id"], "unique": False},
        {"name": "idx_entities_tenant", "column_names": ["tenant_id"], "unique": False},
    ]
    inspector.get_unique_constraints.return_value = [
        {
            "name": "uq_entity_tenant_name",
            "column_names": ["tenant_id", "canonical_name"],
        },
        {
            "name": "uq_entity_tenant_name",
            "column_names": ["tenant_id", "canonical_name"],
        },
    ]
    monkeypatch.setattr(REVISION, "_inspector", lambda: inspector)

    indexes, unique_constraints = REVISION._legacy_dependencies("entities", "tenant_id")

    assert [index["name"] for index in indexes] == ["idx_entities_tenant"]
    assert [constraint["name"] for constraint in unique_constraints] == ["uq_entity_tenant_name"]


@pytest.mark.parametrize(
    "dependency_metadata",
    [
        {"dialect_options": {"postgresql_where": "tenant_id IS NOT NULL"}},
        {"include_columns": ["tenant_id"]},
        {"expressions": ["lower(tenant_id)"]},
        {"column_sorting": {"tenant_id": ("desc",)}},
    ],
)
def test_postgresql_reflection_detects_old_column_in_index_metadata(
    monkeypatch: pytest.MonkeyPatch,
    dependency_metadata: dict[str, object],
) -> None:
    inspector = Mock()
    inspector.get_indexes.return_value = [
        {
            "name": "idx_metadata_dependency",
            "column_names": ["entity_type"],
            "unique": False,
            **dependency_metadata,
        }
    ]
    inspector.get_unique_constraints.return_value = []
    monkeypatch.setattr(REVISION, "_inspector", lambda: inspector)

    indexes, _ = REVISION._legacy_dependencies("entities", "tenant_id")

    assert [index["name"] for index in indexes] == ["idx_metadata_dependency"]


def test_reflected_sql_identifier_transform_preserves_string_literals() -> None:
    predicate = (
        '"tenant_id" IS NOT NULL AND tenant_id <> marker '
        "AND marker <> 'tenant_id' AND note <> 'tenant_id''s value'"
    )

    transformed = REVISION._transform_reflected_value(predicate, "tenant_id", "zone_id")

    assert transformed == (
        '"zone_id" IS NOT NULL AND zone_id <> marker '
        "AND marker <> 'tenant_id' AND note <> 'tenant_id''s value'"
    )


@pytest.mark.parametrize("escape_prefix", ["E", "e"])
def test_reflected_sql_identifier_transform_preserves_postgresql_escape_strings(
    escape_prefix: str,
) -> None:
    predicate = (
        rf"note = {escape_prefix}'tenant_id\' tenant_id' "
        "AND tenant_id IS NOT NULL"
    )

    transformed = REVISION._transform_reflected_value(predicate, "tenant_id", "zone_id")

    assert transformed == (
        rf"note = {escape_prefix}'tenant_id\' tenant_id' "
        "AND zone_id IS NOT NULL"
    )


def _run_mocked_postgresql_mixed_merge(
    monkeypatch: pytest.MonkeyPatch,
    *,
    indexes: list[dict[str, object]],
    unique_constraints: list[dict[str, object]],
) -> tuple[Mock, Mock]:
    inspector = Mock()
    inspector.get_table_names.return_value = ["entities"]
    inspector.get_columns.return_value = [
        {"name": "tenant_id", "type": String(64), "nullable": True},
        {"name": "zone_id", "type": String(64), "nullable": True},
    ]
    inspector.get_indexes.return_value = indexes
    inspector.get_unique_constraints.return_value = unique_constraints

    bind = Mock()
    bind.dialect.name = "postgresql"
    bind.dialect.identifier_preparer.quote.side_effect = lambda identifier: identifier
    monkeypatch.setattr(REVISION, "_inspector", lambda: inspector)
    monkeypatch.setattr(REVISION.op, "get_bind", lambda: bind)
    monkeypatch.setattr(REVISION.op, "execute", Mock())
    monkeypatch.setattr(REVISION.op, "drop_index", Mock())
    monkeypatch.setattr(REVISION.op, "drop_constraint", Mock())
    monkeypatch.setattr(REVISION.op, "drop_column", Mock())
    created_indexes = Mock()
    created_unique_constraints = Mock()
    monkeypatch.setattr(REVISION.op, "create_index", created_indexes)
    monkeypatch.setattr(
        REVISION.op,
        "create_unique_constraint",
        created_unique_constraints,
    )

    REVISION._merge_and_remove_old_column(
        "entities",
        "tenant_id",
        "zone_id",
        "upgrade",
    )

    return created_indexes, created_unique_constraints


def test_postgresql_mixed_merge_skips_custom_name_logical_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_indexes, created_unique_constraints = _run_mocked_postgresql_mixed_merge(
        monkeypatch,
        indexes=[
            {
                "name": "existing_zone_lookup",
                "column_names": ["zone_id", "entity_type"],
                "unique": False,
            },
            {
                "name": "legacy_custom_lookup",
                "column_names": ["tenant_id", "entity_type"],
                "unique": False,
            },
        ],
        unique_constraints=[
            {
                "name": "existing_zone_unique",
                "column_names": ["zone_id", "canonical_name"],
            },
            {
                "name": "legacy_custom_unique",
                "column_names": ["tenant_id", "canonical_name"],
            },
        ],
    )

    created_indexes.assert_not_called()
    created_unique_constraints.assert_not_called()


def test_postgresql_mixed_merge_preserves_distinct_partial_index_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_indexes, _ = _run_mocked_postgresql_mixed_merge(
        monkeypatch,
        indexes=[
            {
                "name": "existing_zone_partial",
                "column_names": ["zone_id", "entity_type"],
                "unique": False,
                "dialect_options": {"postgresql_where": "deleted_at IS NULL"},
            },
            {
                "name": "legacy_tenant_partial",
                "column_names": ["tenant_id", "entity_type"],
                "unique": False,
                "dialect_options": {"postgresql_where": "tenant_id IS NOT NULL"},
            },
            {
                "name": "legacy_tenant_partial_copy",
                "column_names": ["tenant_id", "entity_type"],
                "unique": False,
                "dialect_options": {"postgresql_where": "tenant_id IS NOT NULL"},
            },
        ],
        unique_constraints=[],
    )

    created_indexes.assert_called_once_with(
        "legacy_tenant_partial",
        "entities",
        ["zone_id", "entity_type"],
        unique=False,
        postgresql_where="zone_id IS NOT NULL",
    )


def test_postgresql_mixed_merge_recreates_predicate_only_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_indexes, _ = _run_mocked_postgresql_mixed_merge(
        monkeypatch,
        indexes=[
            {
                "name": "idx_entity_type_for_tenant",
                "column_names": ["entity_type"],
                "unique": False,
                "dialect_options": {
                    "postgresql_where": "tenant_id IS NOT NULL AND marker <> 'tenant_id'"
                },
            }
        ],
        unique_constraints=[],
    )

    created_indexes.assert_called_once_with(
        "idx_entity_type_for_tenant",
        "entities",
        ["entity_type"],
        unique=False,
        postgresql_where="zone_id IS NOT NULL AND marker <> 'tenant_id'",
    )


def test_postgresql_mixed_merge_recreates_reflected_expression_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_indexes, _ = _run_mocked_postgresql_mixed_merge(
        monkeypatch,
        indexes=[
            {
                "name": "idx_lower_tenant",
                "column_names": [None],
                "expressions": ["lower(tenant_id)"],
                "unique": False,
            }
        ],
        unique_constraints=[],
    )

    created_indexes.assert_called_once()
    index_name, table_name, elements = created_indexes.call_args.args
    assert index_name == "idx_lower_tenant"
    assert table_name == "entities"
    assert len(elements) == 1
    assert isinstance(elements[0], type(text("")))
    assert str(elements[0]) == "lower(zone_id)"
    assert created_indexes.call_args.kwargs == {"unique": False}


def test_downgrade_symmetrically_restores_tenant_columns(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'downgrade.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE entities ("
                "entity_id VARCHAR(36) PRIMARY KEY, zone_id VARCHAR(255) NOT NULL)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE relationships ("
                "relationship_id VARCHAR(36) PRIMARY KEY, zone_id VARCHAR(255) NOT NULL)"
            )
        )
        conn.execute(text("INSERT INTO entities VALUES ('e1', 'zone-to-restore')"))

        _run_revision(conn, "downgrade")

        assert "zone_id" not in _column_map(conn, "entities")
        assert getattr(_column_map(conn, "entities")["tenant_id"]["type"], "length", None) == 255
        assert conn.execute(text("SELECT tenant_id FROM entities")).scalar_one() == (
            "zone-to-restore"
        )


@pytest.mark.parametrize(
    ("direction", "old_names", "expected_fragments"),
    [
        (
            "upgrade",
            {
                "indexes": {
                    "entities": {"idx_entities_tenant", "idx_entities_tenant_type"},
                    "relationships": {"idx_relationships_tenant"},
                },
                "constraints": {"entities": {"uq_entity_tenant_name"}},
            },
            (
                "ALTER INDEX idx_entities_tenant RENAME TO idx_entities_zone",
                "ALTER INDEX idx_entities_tenant_type RENAME TO idx_entities_zone_type",
                "ALTER INDEX idx_relationships_tenant RENAME TO idx_relationships_zone",
                "ALTER TABLE entities RENAME CONSTRAINT uq_entity_tenant_name TO uq_entity_zone_name",
            ),
        ),
        (
            "downgrade",
            {
                "indexes": {
                    "entities": {"idx_entities_zone", "idx_entities_zone_type"},
                    "relationships": {"idx_relationships_zone"},
                },
                "constraints": {"entities": {"uq_entity_zone_name"}},
            },
            (
                "ALTER INDEX idx_entities_zone RENAME TO idx_entities_tenant",
                "ALTER INDEX idx_entities_zone_type RENAME TO idx_entities_tenant_type",
                "ALTER INDEX idx_relationships_zone RENAME TO idx_relationships_tenant",
                "ALTER TABLE entities RENAME CONSTRAINT uq_entity_zone_name TO uq_entity_tenant_name",
            ),
        ),
    ],
)
def test_postgresql_schema_object_renames_are_guarded_and_symmetric(
    monkeypatch: pytest.MonkeyPatch,
    direction: str,
    old_names: dict[str, dict[str, set[str]]],
    expected_fragments: tuple[str, ...],
) -> None:
    executed: list[str] = []
    bind = Mock()
    bind.dialect.name = "postgresql"
    monkeypatch.setattr(REVISION.op, "get_bind", lambda: bind)
    monkeypatch.setattr(
        REVISION,
        "_index_names",
        lambda table_name: old_names["indexes"].get(table_name, set()),
    )
    monkeypatch.setattr(
        REVISION,
        "_unique_constraint_names",
        lambda table_name: old_names["constraints"].get(table_name, set()),
    )
    monkeypatch.setattr(REVISION.op, "execute", lambda statement: executed.append(str(statement)))

    REVISION._rename_postgresql_schema_objects(direction=direction)

    assert executed == list(expected_fragments)


def test_postgresql_schema_object_renames_skip_existing_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed: list[str] = []
    bind = Mock()
    bind.dialect.name = "postgresql"
    monkeypatch.setattr(REVISION.op, "get_bind", lambda: bind)
    monkeypatch.setattr(
        REVISION,
        "_index_names",
        lambda _table_name: {
            "idx_entities_tenant",
            "idx_entities_zone",
            "idx_entities_tenant_type",
            "idx_entities_zone_type",
            "idx_relationships_tenant",
            "idx_relationships_zone",
        },
    )
    monkeypatch.setattr(
        REVISION,
        "_unique_constraint_names",
        lambda _table_name: {"uq_entity_tenant_name", "uq_entity_zone_name"},
    )
    monkeypatch.setattr(REVISION.op, "execute", lambda statement: executed.append(str(statement)))

    REVISION._rename_postgresql_schema_objects(direction="upgrade")

    assert executed == []
