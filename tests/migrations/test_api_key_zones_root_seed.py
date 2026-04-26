"""Migration eba93656daab seeds zones.root before backfilling api_key_zones.

Issue #3897: without the seed, an existing install whose api_keys rows
reference zone_ids that don't yet have a zones row blows up at upgrade
time because the junction FK to zones.zone_id rejects the backfill.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text


def _apply_upgrade(conn) -> None:
    """Mirror eba93656daab.upgrade() body inline.

    Order must match the migration: seed → preflight → DDL → backfill.
    SQLite path; PostgreSQL ``CURRENT_TIMESTAMP`` literal works the same.
    """
    conn.execute(
        text("""
        INSERT INTO zones (zone_id, name, phase, finalizers, created_at, updated_at)
        SELECT 'root', 'Root', 'Active', '[]',
               CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        WHERE NOT EXISTS (SELECT 1 FROM zones WHERE zone_id = 'root')
        """)
    )
    orphans = (
        conn.execute(
            text(
                """
                SELECT DISTINCT k.zone_id
                FROM api_keys k
                WHERE k.revoked = 0
                  AND k.zone_id IS NOT NULL
                  AND k.zone_id <> 'root'
                  AND NOT EXISTS (SELECT 1 FROM zones z WHERE z.zone_id = k.zone_id)
                ORDER BY k.zone_id
                """
            )
        )
        .scalars()
        .all()
    )
    if orphans:
        raise RuntimeError(
            "eba93656daab: live api_keys reference zone_ids with no matching "
            "zones row. Create the zones (or revoke the keys) before "
            f"re-running this migration. Offending zone_ids: {sorted(orphans)}"
        )
    conn.execute(
        text("""
        CREATE TABLE api_key_zones (
            key_id VARCHAR(36) NOT NULL,
            zone_id VARCHAR(255) NOT NULL,
            granted_at DATETIME NOT NULL,
            PRIMARY KEY (key_id, zone_id),
            FOREIGN KEY (key_id) REFERENCES api_keys (key_id) ON DELETE CASCADE,
            FOREIGN KEY (zone_id) REFERENCES zones (zone_id) ON DELETE RESTRICT
        )
        """)
    )
    conn.execute(
        text("""
        INSERT INTO api_key_zones (key_id, zone_id, granted_at)
        SELECT key_id, zone_id, created_at FROM api_keys WHERE revoked = 0
        """)
    )


def _build_pre_migration_schema(conn, *, with_root_zone: bool) -> None:
    conn.execute(text("PRAGMA foreign_keys = ON"))
    conn.execute(
        text("""
        CREATE TABLE zones (
            zone_id VARCHAR(255) PRIMARY KEY,
            name VARCHAR(255),
            phase VARCHAR(50),
            finalizers TEXT,
            created_at DATETIME,
            updated_at DATETIME
        )
        """)
    )
    conn.execute(
        text("""
        CREATE TABLE api_keys (
            key_id VARCHAR(36) PRIMARY KEY,
            key_hash VARCHAR(64) NOT NULL,
            user_id VARCHAR(255) NOT NULL,
            name VARCHAR(255) NOT NULL,
            zone_id VARCHAR(255),
            revoked INTEGER DEFAULT 0,
            created_at DATETIME
        )
        """)
    )
    if with_root_zone:
        conn.execute(
            text("INSERT INTO zones (zone_id, name, phase) VALUES ('root', 'Root', 'Active')")
        )


def test_upgrade_seeds_root_zone_on_fresh_install(tmp_path):
    """Fresh DB (no api_keys, no zones) — upgrade leaves zones.root behind."""
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    with engine.begin() as conn:
        _build_pre_migration_schema(conn, with_root_zone=False)
        _apply_upgrade(conn)

    with engine.begin() as conn:
        zones = [r[0] for r in conn.execute(text("SELECT zone_id FROM zones")).all()]
    assert zones == ["root"]


def test_upgrade_rejects_live_keys_with_orphan_zone_ids(tmp_path):
    """Live api_key referencing a non-existent zone must fail the migration.

    Auto-creating the zone would silently bless arbitrary historical or
    corrupt strings as Active tenants. Failing loudly forces a human to
    reconcile the state before continuing.
    """
    import pytest

    engine = create_engine(f"sqlite:///{tmp_path / 'orphans.db'}")
    with engine.begin() as conn:
        _build_pre_migration_schema(conn, with_root_zone=False)
        conn.execute(
            text("""
            INSERT INTO api_keys
              (key_id, key_hash, user_id, name, zone_id, revoked, created_at)
            VALUES
              ('kid_live', 'h1', 'alice', 'alice', 'orphan-zone', 0, '2026-04-01')
            """)
        )

    with engine.begin() as conn, pytest.raises(RuntimeError, match="orphan-zone"):
        _apply_upgrade(conn)


def test_upgrade_ignores_orphan_zones_on_revoked_keys(tmp_path):
    """Only LIVE keys block the migration — revoked keys are irrelevant."""
    engine = create_engine(f"sqlite:///{tmp_path / 'revoked-orphans.db'}")
    with engine.begin() as conn:
        _build_pre_migration_schema(conn, with_root_zone=False)
        conn.execute(
            text("""
            INSERT INTO api_keys
              (key_id, key_hash, user_id, name, zone_id, revoked, created_at)
            VALUES
              ('kid_dead', 'h2', 'bob', 'bob', 'never-seeded', 1, '2026-04-01')
            """)
        )
        _apply_upgrade(conn)

    with engine.begin() as conn:
        zones = sorted(r[0] for r in conn.execute(text("SELECT zone_id FROM zones")).all())
        junction = conn.execute(text("SELECT key_id, zone_id FROM api_key_zones")).all()
    assert zones == ["root"]
    assert junction == []


def test_upgrade_idempotent_when_root_already_seeded(tmp_path):
    """Existing zones.root row is left untouched."""
    engine = create_engine(f"sqlite:///{tmp_path / 'preexisting.db'}")
    with engine.begin() as conn:
        _build_pre_migration_schema(conn, with_root_zone=True)
        # Mark the row so we can detect overwrite.
        conn.execute(text("UPDATE zones SET name = 'untouched' WHERE zone_id = 'root'"))
        _apply_upgrade(conn)

    with engine.begin() as conn:
        name = conn.execute(text("SELECT name FROM zones WHERE zone_id = 'root'")).scalar_one()
    assert name == "untouched"


def test_failed_orphan_preflight_does_not_create_table_so_rerun_succeeds(tmp_path):
    """SQLite regression — preflight failure must leave the schema untouched.

    SQLite doesn't roll back DDL on transaction abort, so if the preflight
    raised after CREATE TABLE the operator would be wedged: rerunning would
    hit "table already exists" instead of completing. Reordering preflight
    *before* DDL makes the fail → fix → rerun cycle clean.
    """
    import pytest

    engine = create_engine(f"sqlite:///{tmp_path / 'rerun.db'}")
    with engine.begin() as conn:
        _build_pre_migration_schema(conn, with_root_zone=False)
        conn.execute(
            text("""
            INSERT INTO api_keys
              (key_id, key_hash, user_id, name, zone_id, revoked, created_at)
            VALUES ('kid_live', 'h1', 'alice', 'alice', 'orphan', 0, '2026-04-01')
            """)
        )

    # First attempt fails on the orphan.
    with engine.begin() as conn, pytest.raises(RuntimeError, match="orphan"):
        _apply_upgrade(conn)

    # Schema invariant: api_key_zones must NOT exist after the failure,
    # otherwise the rerun below would hit "table already exists".
    with engine.begin() as conn:
        tables = [
            r[0]
            for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).all()
        ]
    assert "api_key_zones" not in tables, (
        "preflight raised AFTER create_table — SQLite kept the table around "
        "and the operator can't rerun cleanly"
    )

    # Operator fixes the data: create the missing zone.
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO zones (zone_id, name, phase) VALUES ('orphan', 'orphan', 'Active')")
        )

    # Rerun completes — table now exists, junction backfilled.
    with engine.begin() as conn:
        _apply_upgrade(conn)
    with engine.begin() as conn:
        junction = conn.execute(text("SELECT key_id, zone_id FROM api_key_zones")).all()
    assert junction == [("kid_live", "orphan")]
