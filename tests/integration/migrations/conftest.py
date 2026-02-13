"""Shared fixtures for Alembic migration tests (Issue #1296).

Provides pytest-alembic configuration and engine fixtures for testing
the full migration history: upgrade, downgrade, round-trip, and model-DDL
consistency.

Uses SQLite in-memory for speed. PostgreSQL testing runs in a separate CI job.
When NEXUS_DATABASE_URL is set (e.g. in the PG CI job), that database is used
instead.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

# Project root — alembic.ini lives at <root>/alembic/alembic.ini
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_ALEMBIC_INI = str(_PROJECT_ROOT / "alembic" / "alembic.ini")


@pytest.fixture
def alembic_config():
    """Point pytest-alembic at the project's alembic.ini."""
    return {"file": _ALEMBIC_INI}


@pytest.fixture
def alembic_engine():
    """Create a database engine for migration testing.

    When NEXUS_DATABASE_URL is set (CI PostgreSQL job), uses that.
    Otherwise creates an in-memory SQLite engine with:
    - PRAGMA foreign_keys=ON  — enforces FK constraints
    - PRAGMA busy_timeout=5000 — avoids immediate SQLITE_BUSY errors
    """
    db_url = os.environ.get("NEXUS_DATABASE_URL")

    if db_url:
        engine = create_engine(db_url, isolation_level="AUTOCOMMIT")
        # Clean slate: drop all objects so migrations can re-create them.
        # We drop tables/types/extensions individually rather than using
        # DROP SCHEMA CASCADE, which has edge cases with PG extensions.
        with engine.connect() as conn:
            t = __import__("sqlalchemy").text
            conn.execute(
                t("""
                DO $$ DECLARE r RECORD;
                BEGIN
                    -- Drop all tables
                    FOR r IN (SELECT tablename FROM pg_tables
                              WHERE schemaname = 'public') LOOP
                        EXECUTE 'DROP TABLE IF EXISTS public.'
                                || quote_ident(r.tablename) || ' CASCADE';
                    END LOOP;
                    -- Drop all enum types
                    FOR r IN (SELECT t.typname FROM pg_type t
                              JOIN pg_namespace n ON t.typnamespace = n.oid
                              WHERE n.nspname = 'public' AND t.typtype = 'e') LOOP
                        EXECUTE 'DROP TYPE IF EXISTS public.'
                                || quote_ident(r.typname) || ' CASCADE';
                    END LOOP;
                    -- Drop all views
                    FOR r IN (SELECT viewname FROM pg_views
                              WHERE schemaname = 'public') LOOP
                        EXECUTE 'DROP VIEW IF EXISTS public.'
                                || quote_ident(r.viewname) || ' CASCADE';
                    END LOOP;
                END $$;
                """)
            )
            # Drop extensions so CREATE EXTENSION IF NOT EXISTS works fresh
            for ext in ("vector", "pg_trgm"):
                conn.execute(t(f"DROP EXTENSION IF EXISTS {ext} CASCADE"))
        engine.dispose()
        engine = create_engine(db_url)
    else:
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    try:
        yield engine
    finally:
        engine.dispose()
