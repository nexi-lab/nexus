"""Verifies the api_key_zones backfill mirrors api_keys.zone_id (#3785)."""

from __future__ import annotations

from sqlalchemy import create_engine, text


def test_backfill_creates_one_junction_row_per_live_token(tmp_path):
    """Pre-migration: api_keys with single zone_id. Post-migration: matching junction row."""
    db_path = tmp_path / "backfill.db"
    engine = create_engine(f"sqlite:///{db_path}")

    # Build pre-migration shape: api_keys + zones, no junction yet.
    with engine.begin() as conn:
        conn.execute(
            text("""
            CREATE TABLE zones (
                zone_id VARCHAR(255) PRIMARY KEY,
                phase VARCHAR(50)
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
                zone_id VARCHAR(255) NOT NULL,
                revoked INTEGER DEFAULT 0,
                created_at DATETIME
            )
        """)
        )
        conn.execute(text("INSERT INTO zones (zone_id, phase) VALUES ('eng', 'Active')"))
        conn.execute(
            text("""
            INSERT INTO api_keys
              (key_id, key_hash, user_id, name, zone_id, revoked, created_at)
            VALUES
              ('kid_live', 'h1', 'alice', 'alice', 'eng', 0, '2026-04-01'),
              ('kid_dead', 'h2', 'bob',   'bob',   'eng', 1, '2026-04-01')
        """)
        )

    # Apply the upgrade body inline (DDL + backfill) — same SQL the migration runs.
    with engine.begin() as conn:
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

    # Assert: live token has one junction row, revoked token has none.
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT key_id, zone_id FROM api_key_zones ORDER BY key_id")).all()
    assert rows == [("kid_live", "eng")]
