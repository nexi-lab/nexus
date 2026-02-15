"""Smoke tests for Cloud SQL Python Connector integration.

These tests require a live Cloud SQL instance and are skipped unless
the ``CLOUD_SQL_INSTANCE`` environment variable is set.

Required environment variables:
    CLOUD_SQL_INSTANCE: Instance connection name (project:region:instance)
    CLOUD_SQL_USER:     Database user (default: "nexus")
    CLOUD_SQL_DB:       Database name (default: "nexus")
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from nexus.storage.cloud_sql import create_cloud_sql_creators

_INSTANCE = os.getenv("CLOUD_SQL_INSTANCE", "")
_USER = os.getenv("CLOUD_SQL_USER", "nexus")
_DB = os.getenv("CLOUD_SQL_DB", "nexus")

_skip = pytest.mark.skipif(
    not os.getenv("CLOUD_SQL_INSTANCE"),
    reason="Cloud SQL not configured",
)


def _sync_session_factory() -> sessionmaker[Session]:
    """Build a sync sessionmaker using the Cloud SQL creator."""
    from sqlalchemy import create_engine

    sync_creator, _ = create_cloud_sql_creators(_INSTANCE, _USER, _DB)
    engine = create_engine("postgresql+pg8000://", creator=sync_creator)
    return sessionmaker(bind=engine)


def _async_engine() -> Any:
    """Build an async engine using the Cloud SQL creator."""
    _, async_creator = create_cloud_sql_creators(_INSTANCE, _USER, _DB)
    return create_async_engine("postgresql+asyncpg://", async_creator=async_creator)


@_skip
class TestCloudSQLSmoke:
    """Smoke tests to verify basic Cloud SQL connectivity and features."""

    def test_connect_and_select_one(self) -> None:
        """Verify that a sync connection can execute SELECT 1."""
        factory = _sync_session_factory()
        with factory() as session:
            result = session.execute(text("SELECT 1")).scalar()
            assert result == 1

    def test_pgvector_extension_available(self) -> None:
        """Verify the pgvector extension is loaded in the database."""
        factory = _sync_session_factory()
        with factory() as session:
            row = session.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            ).scalar()
            assert row == 1, "pgvector extension is not installed"

    def test_pg_trgm_extension_available(self) -> None:
        """Verify the pg_trgm extension is loaded in the database."""
        factory = _sync_session_factory()
        with factory() as session:
            row = session.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
            ).scalar()
            assert row == 1, "pg_trgm extension is not installed"

    def test_basic_crud_cycle(self) -> None:
        """Create a table, insert a row, read it back, then drop the table."""
        factory = _sync_session_factory()
        with factory() as session:
            session.execute(text("CREATE TEMP TABLE _smoke_test (id SERIAL PRIMARY KEY, val TEXT)"))
            session.execute(text("INSERT INTO _smoke_test (val) VALUES (:v)"), {"v": "hello"})
            result = session.execute(
                text("SELECT val FROM _smoke_test WHERE val = :v"), {"v": "hello"}
            ).scalar()
            assert result == "hello"

            session.execute(text("DELETE FROM _smoke_test WHERE val = :v"), {"v": "hello"})
            remaining = session.execute(text("SELECT count(*) FROM _smoke_test")).scalar()
            assert remaining == 0

    @pytest.mark.asyncio
    async def test_async_session_works(self) -> None:
        """Verify that the async creator produces a working async session."""
        from sqlalchemy.ext.asyncio import async_sessionmaker

        engine = _async_engine()
        async_factory = async_sessionmaker(engine, class_=AsyncSession)
        async with async_factory() as session:
            result = await session.execute(text("SELECT 1"))
            assert result.scalar() == 1
        await engine.dispose()
