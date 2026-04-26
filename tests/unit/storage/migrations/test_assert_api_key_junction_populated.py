"""Tripwire migration tests (#3871)."""

from __future__ import annotations

import importlib.util
import pathlib

import pytest
from sqlalchemy import create_engine, text


def _load_migration_module():
    # Anchor to the project root via this file's location so pytest-xdist
    # workers (which may run from a different cwd) resolve the path correctly.
    project_root = pathlib.Path(__file__).resolve().parent.parent.parent.parent.parent
    versions = project_root / "alembic" / "versions"
    matches = list(versions.glob("*assert_api_key_junction_populated_for_3871*.py"))
    assert len(matches) == 1, f"Expected one migration file, found {matches}"
    spec = importlib.util.spec_from_file_location("tripwire_3871", matches[0])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_upgrade_against(engine):
    """Run the tripwire's upgrade() against the given engine.

    The migration uses op.get_bind(); we shim that via unittest.mock.patch so
    the test runner never sees a bare type: ignore comment.
    """
    from unittest.mock import patch

    module = _load_migration_module()
    with engine.begin() as conn, patch("alembic.op.get_bind", return_value=conn):
        module.upgrade()


@pytest.fixture
def engine_with_schema():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            text("""
            CREATE TABLE api_keys (
                key_id TEXT PRIMARY KEY,
                revoked INTEGER NOT NULL DEFAULT 0,
                is_admin INTEGER NOT NULL DEFAULT 0
            )
        """)
        )
        conn.execute(
            text("""
            CREATE TABLE api_key_zones (
                key_id TEXT NOT NULL,
                zone_id TEXT NOT NULL,
                PRIMARY KEY (key_id, zone_id)
            )
        """)
        )
    return engine


def test_tripwire_no_op_on_healthy_db(engine_with_schema):
    with engine_with_schema.begin() as conn:
        conn.execute(text("INSERT INTO api_keys (key_id) VALUES ('k1')"))
        conn.execute(text("INSERT INTO api_key_zones (key_id, zone_id) VALUES ('k1', 'eng')"))
    _run_upgrade_against(engine_with_schema)  # must not raise


def test_tripwire_raises_on_broken_db(engine_with_schema):
    with engine_with_schema.begin() as conn:
        conn.execute(text("INSERT INTO api_keys (key_id) VALUES ('orphan')"))
    with pytest.raises(RuntimeError, match="lack junction rows"):
        _run_upgrade_against(engine_with_schema)


def test_tripwire_ignores_admin_keys(engine_with_schema):
    """Admin keys may legitimately have empty junction (zoneless)."""
    with engine_with_schema.begin() as conn:
        conn.execute(text("INSERT INTO api_keys (key_id, is_admin) VALUES ('admin', 1)"))
    _run_upgrade_against(engine_with_schema)  # must not raise


def test_tripwire_ignores_revoked_keys(engine_with_schema):
    with engine_with_schema.begin() as conn:
        conn.execute(text("INSERT INTO api_keys (key_id, revoked) VALUES ('dead', 1)"))
    _run_upgrade_against(engine_with_schema)  # must not raise


def test_tripwire_downgrade_is_noop():
    module = _load_migration_module()
    module.downgrade()  # must not raise
