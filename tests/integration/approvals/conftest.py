"""Shared fixtures for approvals integration tests (Issue #3790)."""

from __future__ import annotations

import os

import asyncpg
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nexus.bricks.approvals.config import ApprovalConfig
from nexus.bricks.approvals.events import NotifyBridge
from nexus.bricks.approvals.repository import ApprovalRepository
from nexus.bricks.approvals.service import ApprovalService


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


@pytest_asyncio.fixture
async def asyncpg_pool():
    raw_url = os.environ.get("NEXUS_TEST_DATABASE_URL", "")
    if raw_url.startswith("postgresql+asyncpg://"):
        raw_url = "postgresql://" + raw_url[len("postgresql+asyncpg://") :]
    if not raw_url:
        raise RuntimeError("NEXUS_TEST_DATABASE_URL must be set for approvals integration tests")
    pool = await asyncpg.create_pool(raw_url, min_size=1, max_size=4)
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def approval_service(session_factory, asyncpg_pool):
    repo = ApprovalRepository(session_factory)
    bridge = NotifyBridge(asyncpg_pool)
    svc = ApprovalService(repo, bridge, ApprovalConfig(enabled=True))
    await svc.start()
    try:
        yield svc
    finally:
        await svc.stop()


@pytest_asyncio.fixture
async def approval_service_short(session_factory, asyncpg_pool):
    repo = ApprovalRepository(session_factory)
    bridge = NotifyBridge(asyncpg_pool)
    svc = ApprovalService(repo, bridge, ApprovalConfig(enabled=True, auto_deny_after_seconds=0.2))
    await svc.start()
    try:
        yield svc
    finally:
        await svc.stop()
