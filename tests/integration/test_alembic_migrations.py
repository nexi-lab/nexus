"""Integration tests for Alembic migrations — Issue #1180 (Consistency Migration).

Tests the `add_zone_consistency_mode` migration which adds a `consistency_mode`
column (String(2), NOT NULL, default='SC') to the `zones` table with a CHECK
constraint limiting values to 'SC' (Strong Consistency) and 'EC' (Eventual
Consistency).

Testing approach:
- Uses SQLite in-memory via SQLAlchemy (no Alembic CLI needed)
- Creates full schema with Base.metadata.create_all(engine)
- Verifies ZoneModel has correct column, default, constraints
- Uses raw SQL for CHECK constraint tests (ORM may bypass CHECK)
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from nexus.storage.models import ZoneModel
from nexus.storage.models._base import Base


@pytest.fixture
def engine():
    """Create an in-memory SQLite engine with CHECK constraint enforcement."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session(engine):
    """Create a database session scoped to each test."""
    with Session(engine) as sess:
        yield sess


def _make_zone(
    zone_id: str = "zone-1",
    name: str = "Test Zone",
    **overrides,
) -> ZoneModel:
    """Build a ZoneModel with sensible defaults; override any field via kwargs."""
    kwargs = {"zone_id": zone_id, "name": name}
    kwargs.update(overrides)
    return ZoneModel(**kwargs)


class TestConsistencyModeColumnExists:
    """Verify the consistency_mode column is present in the zones table schema."""

    def test_zone_model_has_consistency_mode_column(self, engine):
        """The zones table must contain a consistency_mode column after migration."""
        inspector = inspect(engine)
        columns = {col["name"] for col in inspector.get_columns("zones")}
        assert "consistency_mode" in columns, (
            f"consistency_mode column missing from zones table. Found columns: {columns}"
        )

    def test_consistency_mode_column_type_is_string(self, engine):
        """consistency_mode should be a VARCHAR/String column."""
        inspector = inspect(engine)
        columns = {col["name"]: col for col in inspector.get_columns("zones")}
        col = columns["consistency_mode"]
        # SQLite reports VARCHAR; check the type class name
        type_name = str(col["type"]).upper()
        assert "VARCHAR" in type_name or "CHAR" in type_name or "TEXT" in type_name, (
            f"Expected string type for consistency_mode, got: {col['type']}"
        )

    def test_consistency_mode_column_not_nullable(self, engine):
        """consistency_mode must be NOT NULL."""
        inspector = inspect(engine)
        columns = {col["name"]: col for col in inspector.get_columns("zones")}
        col = columns["consistency_mode"]
        assert col["nullable"] is False, "consistency_mode column must be NOT NULL"


class TestConsistencyModeDefault:
    """Verify that the default value for consistency_mode is 'SC'."""

    def test_default_value_is_sc(self, session):
        """A new zone created without explicit consistency_mode should default to 'SC'."""
        zone = _make_zone(zone_id="default-test")
        session.add(zone)
        session.commit()

        session.refresh(zone)
        assert zone.consistency_mode == "SC", (
            f"Expected default consistency_mode 'SC', got '{zone.consistency_mode}'"
        )

    def test_default_value_persists_in_database(self, session):
        """Read back from database to confirm the default was written, not just in-memory."""
        zone = _make_zone(zone_id="persist-test")
        session.add(zone)
        session.commit()

        # Query via raw SQL to bypass ORM defaults
        row = session.execute(
            text("SELECT consistency_mode FROM zones WHERE zone_id = :zid"),
            {"zid": "persist-test"},
        ).fetchone()
        assert row is not None
        assert row[0] == "SC"


class TestValidConsistencyModes:
    """Verify that valid consistency mode values are accepted."""

    def test_new_zone_with_sc(self, session):
        """INSERT with consistency_mode='SC' should succeed."""
        zone = _make_zone(zone_id="sc-zone", consistency_mode="SC")
        session.add(zone)
        session.commit()

        session.refresh(zone)
        assert zone.consistency_mode == "SC"

    def test_new_zone_with_ec(self, session):
        """INSERT with consistency_mode='EC' should succeed."""
        zone = _make_zone(zone_id="ec-zone", consistency_mode="EC")
        session.add(zone)
        session.commit()

        session.refresh(zone)
        assert zone.consistency_mode == "EC"


class TestInvalidConsistencyModeRejected:
    """Verify that the CHECK constraint rejects invalid consistency_mode values.

    These tests use raw SQL because SQLAlchemy ORM may not trigger CHECK
    constraints during in-Python validation.
    """

    def test_invalid_mode_rejected_via_raw_sql(self, engine):
        """INSERT with an invalid consistency_mode value must fail at the DB level."""
        with engine.connect() as conn:
            # Insert a valid zone first to confirm the table works
            conn.execute(
                text(
                    "INSERT INTO zones (zone_id, name, is_active, consistency_mode, "
                    "created_at, updated_at) "
                    "VALUES (:zid, :name, 1, 'SC', datetime('now'), datetime('now'))"
                ),
                {"zid": "valid-zone", "name": "Valid Zone"},
            )
            conn.commit()

            # Now try an invalid consistency_mode
            with pytest.raises(IntegrityError):
                conn.execute(
                    text(
                        "INSERT INTO zones (zone_id, name, is_active, consistency_mode, "
                        "created_at, updated_at) "
                        "VALUES (:zid, :name, 1, :mode, datetime('now'), datetime('now'))"
                    ),
                    {"zid": "bad-zone", "name": "Bad Zone", "mode": "INVALID"},
                )
                conn.commit()

    def test_empty_string_rejected_via_raw_sql(self, engine):
        """INSERT with consistency_mode='' must fail (not a valid 2-char code)."""
        with engine.connect() as conn, pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO zones (zone_id, name, is_active, consistency_mode, "
                    "created_at, updated_at) "
                    "VALUES (:zid, :name, 1, :mode, datetime('now'), datetime('now'))"
                ),
                {"zid": "empty-mode", "name": "Empty Mode", "mode": ""},
            )
            conn.commit()

    def test_null_consistency_mode_rejected_via_raw_sql(self, engine):
        """INSERT with consistency_mode=NULL must fail (NOT NULL constraint)."""
        with engine.connect() as conn, pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO zones (zone_id, name, is_active, consistency_mode, "
                    "created_at, updated_at) "
                    "VALUES (:zid, :name, 1, NULL, datetime('now'), datetime('now'))"
                ),
                {"zid": "null-mode", "name": "Null Mode"},
            )
            conn.commit()


class TestExistingZoneDataPreserved:
    """Verify that existing zone data is not corrupted by the new column."""

    def test_existing_zones_preserved(self, session):
        """Insert a zone, verify all original fields survive alongside the new column."""
        zone = _make_zone(
            zone_id="preserve-1",
            name="Preserved Zone",
            domain="preserve.example.com",
            description="A zone that should survive migration",
        )
        session.add(zone)
        session.commit()

        fetched = session.get(ZoneModel, "preserve-1")
        assert fetched is not None
        assert fetched.name == "Preserved Zone"
        assert fetched.domain == "preserve.example.com"
        assert fetched.description == "A zone that should survive migration"
        assert fetched.is_active == 1
        assert fetched.consistency_mode == "SC"

    def test_zone_settings_column_unchanged(self, session):
        """The settings JSON column should still work correctly with the new column present."""
        settings_data = json.dumps({"max_file_size": 1048576, "allow_public": False})
        zone = _make_zone(
            zone_id="settings-test",
            settings=settings_data,
        )
        session.add(zone)
        session.commit()

        fetched = session.get(ZoneModel, "settings-test")
        assert fetched is not None
        assert fetched.settings is not None
        parsed = json.loads(fetched.settings)
        assert parsed["max_file_size"] == 1048576
        assert parsed["allow_public"] is False
        # The new column should coexist without affecting settings
        assert fetched.consistency_mode == "SC"


class TestQueryByConsistencyMode:
    """Verify that zones can be filtered by consistency_mode."""

    def test_zone_model_query_by_consistency_mode(self, session):
        """Filtering WHERE consistency_mode = 'EC' should return only EC zones."""
        sc_zone = _make_zone(zone_id="sc-query", name="SC Zone", consistency_mode="SC")
        ec_zone = _make_zone(zone_id="ec-query", name="EC Zone", consistency_mode="EC")
        session.add_all([sc_zone, ec_zone])
        session.commit()

        ec_results = session.query(ZoneModel).filter(ZoneModel.consistency_mode == "EC").all()
        assert len(ec_results) == 1
        assert ec_results[0].zone_id == "ec-query"
        assert ec_results[0].name == "EC Zone"

    def test_query_sc_zones(self, session):
        """Filtering WHERE consistency_mode = 'SC' should return only SC zones."""
        sc1 = _make_zone(zone_id="sc-1", name="SC One", consistency_mode="SC")
        sc2 = _make_zone(zone_id="sc-2", name="SC Two", consistency_mode="SC")
        ec1 = _make_zone(zone_id="ec-1", name="EC One", consistency_mode="EC")
        session.add_all([sc1, sc2, ec1])
        session.commit()

        sc_results = session.query(ZoneModel).filter(ZoneModel.consistency_mode == "SC").all()
        sc_ids = {z.zone_id for z in sc_results}
        assert sc_ids == {"sc-1", "sc-2"}


class TestZoneRepr:
    """Verify that the __repr__ method includes consistency_mode."""

    def test_zone_repr_includes_consistency_mode(self, session):
        """ZoneModel.__repr__ should include the consistency_mode value."""
        zone = _make_zone(zone_id="repr-zone", name="Repr Zone", consistency_mode="EC")
        session.add(zone)
        session.commit()

        repr_str = repr(zone)
        assert "consistency_mode" in repr_str, (
            f"__repr__ should include consistency_mode. Got: {repr_str}"
        )
        assert "EC" in repr_str, f"__repr__ should show the actual mode value 'EC'. Got: {repr_str}"


class TestConsistencyModeUpdate:
    """Verify that consistency_mode can be updated on an existing zone."""

    def test_update_sc_to_ec(self, session):
        """Updating consistency_mode from 'SC' to 'EC' should persist correctly."""
        zone = _make_zone(zone_id="update-test")
        session.add(zone)
        session.commit()

        assert zone.consistency_mode == "SC"

        # Update: create new state (immutability principle at app level,
        # but ORM update is the standard SQLAlchemy pattern)
        zone.consistency_mode = "EC"
        session.commit()

        fetched = session.get(ZoneModel, "update-test")
        assert fetched is not None
        assert fetched.consistency_mode == "EC"

    def test_update_ec_to_sc(self, session):
        """Updating consistency_mode from 'EC' back to 'SC' should work."""
        zone = _make_zone(zone_id="revert-test", consistency_mode="EC")
        session.add(zone)
        session.commit()

        zone.consistency_mode = "SC"
        session.commit()

        fetched = session.get(ZoneModel, "revert-test")
        assert fetched is not None
        assert fetched.consistency_mode == "SC"
