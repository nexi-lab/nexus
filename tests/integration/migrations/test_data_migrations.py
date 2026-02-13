"""Data migration tests — template + concrete example (Issue #1296).

Pattern for testing data migrations that transform existing data:

    1. alembic_runner.migrate_up_to("target_revision")
       — Advance to the migration under test.

    2. Insert sample data via raw SQL (NOT the ORM, since the ORM reflects
       the *current* model state which may not match the schema at that
       revision).

    3. Assert data was transformed correctly via raw SQL.

    4. alembic_runner.migrate_down_to("parent_revision")
       — Revert the migration.

    5. Assert data reverted correctly (or table removed, etc.).

Why raw SQL?
    The SQLAlchemy ORM models reflect the current HEAD schema. At an
    intermediate revision the schema may differ — columns missing, types
    changed, etc. Raw SQL avoids this mismatch.

Why migrate_up_to / migrate_down_to (not migrate_up_one / migrate_down_one)?
    With branched DAGs, "one step" may land on a different branch than
    expected. Explicit revision targets avoid this ambiguity.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


class TestPersistentNamespaceViewsMigration:
    """Test the add_persistent_namespace_views migration (Issue #1265).

    This migration creates the persistent_namespace_views table with:
    - id (PK), subject_type, subject_id, zone_id, mount_paths_json,
      grants_hash, revision_bucket, created_at, updated_at
    - Unique constraint on (subject_type, subject_id, zone_id)
    - Index on zone_id

    Revision: 'add_persistent_namespace_views'
    Parent:   'add_agent_events_table'
    """

    TARGET_REVISION = "add_persistent_namespace_views"
    PARENT_REVISION = "add_agent_events_table"

    def test_upgrade_creates_table_and_accepts_data(self, alembic_runner, alembic_engine):
        """Upgrade creates table; inserted data is queryable."""
        alembic_runner.migrate_up_to(self.TARGET_REVISION)

        row_id = str(uuid.uuid4())
        with alembic_engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO persistent_namespace_views "
                    "(id, subject_type, subject_id, zone_id, mount_paths_json, "
                    "grants_hash, revision_bucket, created_at, updated_at) "
                    "VALUES (:id, :stype, :sid, :zid, :mpj, :gh, :rb, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "id": row_id,
                    "stype": "agent",
                    "sid": "agent-001",
                    "zid": "test-zone",
                    "mpj": '["/workspace/agent-001"]',
                    "gh": "abc123",
                    "rb": 1,
                },
            )
            conn.commit()

            row = conn.execute(
                text(
                    "SELECT subject_type, subject_id, zone_id FROM "
                    "persistent_namespace_views WHERE id = :id"
                ),
                {"id": row_id},
            ).fetchone()

            assert row is not None, "Inserted row should be queryable"
            assert row[0] == "agent"
            assert row[1] == "agent-001"
            assert row[2] == "test-zone"

    def test_unique_constraint_enforced(self, alembic_runner, alembic_engine):
        """Unique constraint on (subject_type, subject_id, zone_id) rejects duplicates."""
        alembic_runner.migrate_up_to(self.TARGET_REVISION)

        with alembic_engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO persistent_namespace_views "
                    "(id, subject_type, subject_id, zone_id, mount_paths_json, "
                    "grants_hash, revision_bucket, created_at, updated_at) "
                    "VALUES (:id, :stype, :sid, :zid, :mpj, :gh, :rb, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "stype": "agent",
                    "sid": "agent-001",
                    "zid": "zone-a",
                    "mpj": "[]",
                    "gh": "hash1",
                    "rb": 1,
                },
            )
            conn.commit()

            # Duplicate (same subject_type + subject_id + zone_id) should fail
            with pytest.raises(IntegrityError):
                conn.execute(
                    text(
                        "INSERT INTO persistent_namespace_views "
                        "(id, subject_type, subject_id, zone_id, mount_paths_json, "
                        "grants_hash, revision_bucket, created_at, updated_at) "
                        "VALUES (:id, :stype, :sid, :zid, :mpj, :gh, :rb, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "stype": "agent",
                        "sid": "agent-001",
                        "zid": "zone-a",
                        "mpj": "[]",
                        "gh": "hash2",
                        "rb": 2,
                    },
                )
                conn.commit()

    def test_downgrade_removes_table(self, alembic_runner, alembic_engine):
        """Downgrade drops the persistent_namespace_views table entirely."""
        alembic_runner.migrate_up_to(self.TARGET_REVISION)

        with alembic_engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO persistent_namespace_views "
                    "(id, subject_type, subject_id, zone_id, mount_paths_json, "
                    "grants_hash, revision_bucket, created_at, updated_at) "
                    "VALUES (:id, :stype, :sid, :zid, :mpj, :gh, :rb, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "stype": "user",
                    "sid": "user-99",
                    "zid": "default",
                    "mpj": "[]",
                    "gh": "xyz",
                    "rb": 0,
                },
            )
            conn.commit()

        # Downgrade to parent revision (drops the table)
        alembic_runner.migrate_down_to(self.PARENT_REVISION)

        inspector = sa_inspect(alembic_engine)
        table_names = inspector.get_table_names()
        assert "persistent_namespace_views" not in table_names, (
            "persistent_namespace_views table should not exist after downgrade"
        )

    def test_zone_id_defaults_to_default(self, alembic_runner, alembic_engine):
        """The zone_id column should default to 'default' via server_default."""
        alembic_runner.migrate_up_to(self.TARGET_REVISION)

        with alembic_engine.connect() as conn:
            row_id = str(uuid.uuid4())
            conn.execute(
                text(
                    "INSERT INTO persistent_namespace_views "
                    "(id, subject_type, subject_id, mount_paths_json, "
                    "grants_hash, revision_bucket, created_at, updated_at) "
                    "VALUES (:id, :stype, :sid, :mpj, :gh, :rb, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "id": row_id,
                    "stype": "agent",
                    "sid": "agent-default-zone",
                    "mpj": "[]",
                    "gh": "def",
                    "rb": 0,
                },
            )
            conn.commit()

            row = conn.execute(
                text("SELECT zone_id FROM persistent_namespace_views WHERE id = :id"),
                {"id": row_id},
            ).fetchone()

            assert row is not None
            assert row[0] == "default", f"Expected zone_id default 'default', got '{row[0]}'"
