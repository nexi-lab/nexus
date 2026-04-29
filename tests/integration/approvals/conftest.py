"""Shared fixtures for approvals integration tests (Issue #3790)."""

from __future__ import annotations

import os

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def _db_url() -> str:
    url = os.environ.get("NEXUS_TEST_DATABASE_URL")
    if not url:
        raise RuntimeError("NEXUS_TEST_DATABASE_URL must be set for approvals integration tests")
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(_db_url())
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()
