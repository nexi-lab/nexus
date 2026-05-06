"""Shared fixtures for search integration tests.

Provides:
- sqlite_engine_after_upgrade: sync SQLite Engine with full schema + alembic
  upgrade head, used by test_migration_cutover.py to verify the FTS5 migration.
- postgres_engine: async SQLAlchemy Engine for Postgres integration tests.
  Requires NEXUS_TEST_DATABASE_URL env var; skips if absent.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool

# Project root — alembic.ini lives at <root>/alembic/alembic.ini
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
_ALEMBIC_INI = str(_PROJECT_ROOT / "alembic" / "alembic.ini")


@pytest.fixture(scope="session")
def sqlite_engine_after_upgrade(tmp_path_factory):
    """Sync SQLite Engine after running alembic upgrade head.

    Steps:
    1. Create a temp SQLite file.
    2. Create the schema via Base.metadata.create_all (which skips FTS5 vtable
       and triggers — those are migration-only).
    3. Stamp the DB at the revision just before our new migration so that
       alembic upgrade only runs 25980632a418 (avoids Postgres-only DDL in
       the full chain).
    4. Run alembic upgrade 25980632a418 so the SQLite FTS5 branch fires.
    5. Yield the engine for tests to use.
    """
    from alembic.config import Config as AlembicConfig

    from alembic import command as alembic_command
    from nexus.storage.models import Base

    tmp_dir = tmp_path_factory.mktemp("migration_sqlite")
    db_path = tmp_dir / "test_migration.db"
    db_url = f"sqlite:///{db_path}"

    engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    # Step 1: Create all ORM-managed tables (file_paths, document_chunks, etc.).
    # FTS5 vtable + triggers are NOT in the ORM metadata — they come from migration.
    Base.metadata.create_all(engine)

    # Step 2: Stamp the alembic_version table at the revision just before our
    # new migration. This prevents alembic from re-running the full chain which
    # contains Postgres-only SQL (CREATE EXTENSION vector, halfvec columns, etc.).
    with engine.connect() as conn:
        conn.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        conn.execute(
            text(
                "INSERT INTO alembic_version (version_num) VALUES ('rename_bypass_tenant_to_zone')"
            )
        )
        conn.commit()

    # Step 3: Run only the new migration (25980632a418) via alembic's programmatic
    # API, injecting our engine connection through config.attributes so env.py
    # uses it instead of the alembic.ini URL.
    alembic_cfg = AlembicConfig(_ALEMBIC_INI)
    migration_conn = engine.connect()
    alembic_cfg.attributes["connection"] = migration_conn
    try:
        alembic_command.upgrade(alembic_cfg, "25980632a418")
        migration_conn.commit()
    finally:
        migration_conn.close()

    yield engine

    engine.dispose()


@pytest_asyncio.fixture
async def postgres_engine():
    """Async SQLAlchemy engine for Postgres integration tests.

    Requires NEXUS_TEST_DATABASE_URL (or NEXUS_DATABASE_URL / POSTGRES_URL)
    pointing at a live Postgres instance. The test is skipped when none of
    those variables are set.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    db_url = (
        os.environ.get("NEXUS_TEST_DATABASE_URL")
        or os.environ.get("NEXUS_DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
    )
    if not db_url:
        pytest.skip(
            "No Postgres URL configured. "
            "Set NEXUS_TEST_DATABASE_URL to run Postgres migration tests."
        )

    # Ensure we use the asyncpg driver.
    if db_url.startswith("postgresql://"):
        db_url = "postgresql+asyncpg://" + db_url[len("postgresql://") :]
    elif db_url.startswith("postgres://"):
        db_url = "postgresql+asyncpg://" + db_url[len("postgres://") :]

    engine = create_async_engine(db_url, echo=False)
    try:
        yield engine
    finally:
        await engine.dispose()
