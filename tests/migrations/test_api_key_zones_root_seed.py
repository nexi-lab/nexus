"""Migration eba93656daab seeds zones.root before backfilling api_key_zones.

Issue #3897: without the seed, an existing install whose api_keys rows
reference zone_ids that don't yet have a zones row blows up at upgrade
time because the junction FK to zones.zone_id rejects the backfill.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text


def _apply_upgrade(conn) -> None:
    """Mirror eba93656daab.upgrade() body inline.

    SQLite path; PostgreSQL ``CURRENT_TIMESTAMP`` literal works the same.
    """
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
        INSERT INTO zones (zone_id, name, phase, finalizers, created_at, updated_at)
        SELECT 'root', 'Root', 'Active', '[]',
               CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        WHERE NOT EXISTS (SELECT 1 FROM zones WHERE zone_id = 'root')
        """)
    )
    conn.execute(
        text("""
        INSERT INTO zones (zone_id, name, phase, finalizers, created_at, updated_at)
        SELECT DISTINCT k.zone_id, k.zone_id, 'Active', '[]',
               CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        FROM api_keys k
        WHERE k.revoked = 0
          AND k.zone_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM zones z WHERE z.zone_id = k.zone_id)
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


def test_upgrade_seeds_orphan_zone_ids_referenced_by_live_keys(tmp_path):
    """Pre-existing api_keys reference zones that don't exist — upgrade backfills."""
    engine = create_engine(f"sqlite:///{tmp_path / 'orphans.db'}")
    with engine.begin() as conn:
        _build_pre_migration_schema(conn, with_root_zone=False)
        conn.execute(
            text("""
            INSERT INTO api_keys
              (key_id, key_hash, user_id, name, zone_id, revoked, created_at)
            VALUES
              ('kid_live', 'h1', 'alice', 'alice', 'orphan-zone', 0, '2026-04-01'),
              ('kid_dead', 'h2', 'bob',   'bob',   'never-seeded', 1, '2026-04-01')
            """)
        )
        # Pre-flight: backfilling api_key_zones with FK enabled would fail
        # because 'orphan-zone' has no zones row.  The upgrade must seed it.
        _apply_upgrade(conn)

    with engine.begin() as conn:
        zones = sorted(r[0] for r in conn.execute(text("SELECT zone_id FROM zones")).all())
        junction = sorted(
            tuple(r) for r in conn.execute(text("SELECT key_id, zone_id FROM api_key_zones")).all()
        )
    # 'orphan-zone' seeded; 'never-seeded' is from a revoked key so we don't
    # synthesize it. 'root' is always seeded.
    assert zones == ["orphan-zone", "root"]
    assert junction == [("kid_live", "orphan-zone")]


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
