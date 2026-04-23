"""Integration tests for read-path additions to PostgresAuthProfileStore (#3818).

Requires a running Postgres (env: NEXUS_TEST_DATABASE_URL). Skip cleanly when absent.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from nexus.bricks.auth.postgres_profile_store import (
    ensure_schema,
)


@pytest.fixture
def engine():
    url = os.environ.get("NEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("NEXUS_TEST_DATABASE_URL not set")
    eng = create_engine(url, future=True)
    ensure_schema(eng)
    yield eng
    eng.dispose()


def test_auth_profile_reads_table_exists(engine):
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'auth_profile_reads' ORDER BY ordinal_position"
            )
        ).fetchall()
    cols = [r[0] for r in rows]
    assert cols == [
        "id",
        "read_at",
        "tenant_id",
        "principal_id",
        "auth_profile_id",
        "caller_machine_id",
        "caller_kind",
        "provider",
        "purpose",
        "cache_hit",
        "kek_version",
    ]


def test_auth_profile_reads_has_rls_enabled(engine):
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = 'auth_profile_reads'"
            )
        ).fetchone()
    assert row == (True, True)
