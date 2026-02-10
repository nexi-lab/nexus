"""E2E tests for Scheduler API with real FastAPI server + PostgreSQL.

Tests the scheduler through the full create_app() server stack:
- Real create_app() with auth dependency override
- PostgreSQL database (requires Docker container on localhost:5432)
- Real asyncpg connection pool for SchedulerService
- Real TaskQueue executing SQL against PostgreSQL
- CreditsService(enabled=False) pass-through mode
- DatabaseAPIKeyAuth for auth_type=database tests
- Full lifecycle: auth → scheduler router → SchedulerService → PostgreSQL → response
- Performance validation: latency and throughput

Issue #1212: Add hybrid priority system for task scheduling

Requirements:
- PostgreSQL running on localhost:5432 (use docker-compose or local install)
- Database: scorpio (user: scorpio, password: scorpio) or set TEST_DATABASE_URL
"""

from __future__ import annotations

import os
import socket
import time
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock

import asyncpg
import pytest
import pytest_asyncio
from fastapi import Header
from httpx import ASGITransport, AsyncClient

from nexus.pay.credits import CreditsService
from nexus.scheduler.queue import TaskQueue
from nexus.scheduler.service import SchedulerService


def _pg_available() -> bool:
    """Check if PostgreSQL is reachable at localhost:5432."""
    try:
        with socket.create_connection(("localhost", 5432), timeout=1):
            return True
    except OSError:
        return False


PG_AVAILABLE = _pg_available()

# PostgreSQL test database URL (asyncpg native)
TEST_PG_DSN = os.getenv("TEST_PG_DSN", "postgresql://scorpio:scorpio@localhost:5432/scorpio")

# Sync URL for create_app()
TEST_SYNC_DB_URL = TEST_PG_DSN

# Skip all tests if PostgreSQL is not available
pytestmark = [
    pytest.mark.xdist_group("scheduler_server_e2e"),
    pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL not available at localhost:5432"),
]


# =============================================================================
# SQL for table setup
# =============================================================================

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id TEXT NOT NULL,
    executor_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    priority_tier SMALLINT NOT NULL DEFAULT 2,
    deadline TIMESTAMPTZ,
    boost_amount NUMERIC(12,6) NOT NULL DEFAULT 0,
    boost_tiers SMALLINT NOT NULL DEFAULT 0,
    effective_tier SMALLINT NOT NULL DEFAULT 2,
    enqueued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'queued',
    boost_reservation_id TEXT,
    idempotency_key TEXT UNIQUE,
    zone_id TEXT NOT NULL DEFAULT 'default',
    error_message TEXT
)
"""

_CREATE_DEQUEUE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_dequeue
ON scheduled_tasks (effective_tier, enqueued_at)
WHERE status = 'queued'
"""

_CREATE_AGENT_INDEX = """
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_agent_status
ON scheduled_tasks (agent_id, status)
"""


# =============================================================================
# Fixtures
# =============================================================================


@pytest_asyncio.fixture
async def asyncpg_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    """Real asyncpg connection pool for SchedulerService.

    Creates the scheduled_tasks table and truncates on setup/teardown.
    """
    pool = await asyncpg.create_pool(TEST_PG_DSN, min_size=2, max_size=5)

    async with pool.acquire() as conn:
        await conn.execute(_CREATE_TABLE)
        await conn.execute(_CREATE_DEQUEUE_INDEX)
        await conn.execute(_CREATE_AGENT_INDEX)
        await conn.execute("TRUNCATE scheduled_tasks CASCADE")

    yield pool

    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE scheduled_tasks CASCADE")

    await pool.close()


@pytest.fixture
def scheduler_service(asyncpg_pool: asyncpg.Pool) -> SchedulerService:
    """Real SchedulerService with PostgreSQL pool and disabled credits."""
    return SchedulerService(
        queue=TaskQueue(),
        db_pool=asyncpg_pool,
        credits_service=CreditsService(enabled=False),
    )


@pytest_asyncio.fixture
async def client(
    scheduler_service: SchedulerService,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[AsyncClient, None]:
    """Full create_app() server with scheduler service and auth override.

    Replicates the production server stack for scheduler:
    1. create_app() with real routes
    2. Real SchedulerService with PostgreSQL pool
    3. Auth dependency override (authenticated user)
    """
    monkeypatch.setenv("NEXUS_SEARCH_DAEMON", "false")

    mock_nexus_fs = MagicMock()
    mock_nexus_fs._event_bus = None
    mock_nexus_fs._coordination_client = None

    from nexus.server.fastapi_server import _app_state, create_app, get_auth_result

    app = create_app(
        nexus_fs=mock_nexus_fs,
        database_url=TEST_SYNC_DB_URL,
    )

    # Wire scheduler service into app state
    app.state.scheduler_service = scheduler_service

    # Override auth to return an authenticated user
    async def mock_auth_result(
        authorization: str | None = Header(None, alias="Authorization"),  # noqa: ARG001
        x_agent_id: str | None = Header(None, alias="X-Agent-ID"),  # noqa: ARG001
        x_nexus_subject: str | None = Header(None, alias="X-Nexus-Subject"),  # noqa: ARG001
        x_nexus_zone_id: str | None = Header(None, alias="X-Nexus-Zone-ID"),  # noqa: ARG001
    ) -> dict[str, Any]:
        return {
            "authenticated": True,
            "subject_type": "user",
            "subject_id": "test_user",
            "zone_id": "test-tenant",
            "is_admin": False,
            "inherit_permissions": True,
        }

    app.dependency_overrides[get_auth_result] = mock_auth_result

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as http_client:
        yield http_client

    _app_state.async_nexus_fs = None
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def db_auth_client(
    scheduler_service: SchedulerService,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[tuple[AsyncClient, str], None]:
    """Server with DatabaseAPIKeyAuth (auth_type=database) for scheduler.

    Creates a real DatabaseAPIKeyAuth provider, provisions a test API key
    in PostgreSQL, and returns the client + raw API key for Bearer auth.
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from nexus.server.auth.database_key import DatabaseAPIKeyAuth
    from nexus.storage.models import APIKeyModel

    monkeypatch.setenv("NEXUS_SEARCH_DAEMON", "false")

    sync_engine = create_engine(TEST_SYNC_DB_URL, echo=False)
    SessionFactory = sessionmaker(bind=sync_engine)

    with sync_engine.begin() as conn:
        APIKeyModel.__table__.create(conn, checkfirst=True)

    with SessionFactory() as session:
        key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="scheduler_test_user",
            name="Scheduler E2E Test Key",
            subject_type="user",
            subject_id="sched_test_agent",
            zone_id="test-tenant",
            is_admin=False,
        )
        session.commit()

    db_auth = DatabaseAPIKeyAuth(SessionFactory)

    mock_nexus_fs = MagicMock()
    mock_nexus_fs._event_bus = None
    mock_nexus_fs._coordination_client = None

    from nexus.server.fastapi_server import _app_state, create_app

    app = create_app(
        nexus_fs=mock_nexus_fs,
        database_url=TEST_SYNC_DB_URL,
        auth_provider=db_auth,
    )
    app.state.scheduler_service = scheduler_service

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as http_client:
        yield (http_client, raw_key)

    _app_state.async_nexus_fs = None

    with SessionFactory() as session:
        session.execute(
            text("DELETE FROM api_keys WHERE key_id = :key_id"),
            {"key_id": key_id},
        )
        session.commit()

    sync_engine.dispose()


# =============================================================================
# 1. Basic Submit-Get-Cancel (correctness)
# =============================================================================


@pytest.mark.asyncio
async def test_submit_task_returns_201(client: AsyncClient) -> None:
    """Submit task returns 201 with correct response body."""
    resp = await client.post(
        "/api/v2/scheduler/submit",
        json={
            "executor": "agent-worker",
            "task_type": "process",
            "payload": {"data": "test"},
            "priority": "normal",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "queued"
    assert data["executor_id"] == "agent-worker"
    assert data["task_type"] == "process"
    assert data["priority_tier"] == "normal"
    assert data["effective_tier"] == 2
    assert data["id"]  # UUID assigned


@pytest.mark.asyncio
async def test_get_task_status(client: AsyncClient) -> None:
    """Submit then GET returns correct status."""
    resp = await client.post(
        "/api/v2/scheduler/submit",
        json={"executor": "worker", "task_type": "analyze"},
    )
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    resp = await client.get(f"/api/v2/scheduler/task/{task_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == task_id
    assert data["status"] == "queued"
    assert data["task_type"] == "analyze"


@pytest.mark.asyncio
async def test_cancel_task(client: AsyncClient) -> None:
    """Submit then cancel returns cancelled=true."""
    resp = await client.post(
        "/api/v2/scheduler/submit",
        json={"executor": "worker", "task_type": "cancel_me"},
    )
    task_id = resp.json()["id"]

    resp = await client.post(f"/api/v2/scheduler/task/{task_id}/cancel")
    assert resp.status_code == 200
    data = resp.json()
    assert data["cancelled"] is True
    assert data["task_id"] == task_id


@pytest.mark.asyncio
async def test_get_nonexistent_task_returns_404(client: AsyncClient) -> None:
    """GET unknown task ID returns 404."""
    resp = await client.get("/api/v2/scheduler/task/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


# =============================================================================
# 2. Priority & Boost
# =============================================================================


@pytest.mark.asyncio
async def test_submit_with_critical_priority(client: AsyncClient) -> None:
    """Submit with critical priority sets effective_tier=0."""
    resp = await client.post(
        "/api/v2/scheduler/submit",
        json={"executor": "worker", "task_type": "urgent", "priority": "critical"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["priority_tier"] == "critical"
    assert data["effective_tier"] == 0


@pytest.mark.asyncio
async def test_submit_with_boost(client: AsyncClient) -> None:
    """Submit LOW with boost lowers effective_tier."""
    resp = await client.post(
        "/api/v2/scheduler/submit",
        json={
            "executor": "worker",
            "task_type": "boosted",
            "priority": "low",
            "boost": "0.02",  # 2 tiers boost (BOOST_COST_PER_TIER = 0.01)
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["priority_tier"] == "low"
    # LOW(3) - 2 boost = 1 (HIGH)
    assert data["effective_tier"] == 1


@pytest.mark.asyncio
async def test_invalid_priority_returns_422(client: AsyncClient) -> None:
    """Submit with invalid priority returns 422."""
    resp = await client.post(
        "/api/v2/scheduler/submit",
        json={"executor": "worker", "task_type": "bad", "priority": "garbage"},
    )
    assert resp.status_code == 422


# =============================================================================
# 3. Auth Enforcement
# =============================================================================


@pytest.mark.asyncio
async def test_submit_requires_auth(
    db_auth_client: tuple[AsyncClient, str],
) -> None:
    """Submit returns 401 without Bearer token (DatabaseAPIKeyAuth)."""
    http_client, _ = db_auth_client
    resp = await http_client.post(
        "/api/v2/scheduler/submit",
        json={"executor": "worker", "task_type": "test"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_submit_with_valid_db_key(
    db_auth_client: tuple[AsyncClient, str],
) -> None:
    """Submit succeeds with valid database-backed API key."""
    http_client, raw_key = db_auth_client
    resp = await http_client.post(
        "/api/v2/scheduler/submit",
        json={"executor": "worker", "task_type": "auth_test"},
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "queued"
    assert data["task_type"] == "auth_test"


# =============================================================================
# 4. Full Lifecycle
# =============================================================================


@pytest.mark.asyncio
async def test_full_lifecycle_submit_get_cancel(client: AsyncClient) -> None:
    """Full lifecycle: submit → GET (queued) → cancel → GET (cancelled).

    Validates database state transitions through the full server stack.
    """
    # 1. Submit
    resp = await client.post(
        "/api/v2/scheduler/submit",
        json={"executor": "worker", "task_type": "lifecycle"},
    )
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    # 2. Verify queued
    resp = await client.get(f"/api/v2/scheduler/task/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"

    # 3. Cancel
    resp = await client.post(f"/api/v2/scheduler/task/{task_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["cancelled"] is True

    # 4. Verify cancelled in DB
    resp = await client.get(f"/api/v2/scheduler/task/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_db_auth_full_lifecycle(
    db_auth_client: tuple[AsyncClient, str],
) -> None:
    """Full lifecycle with DatabaseAPIKeyAuth: submit → get → cancel.

    Validates the auth_type=database flow through the real server:
    1. DatabaseAPIKeyAuth validates sk- key in PostgreSQL
    2. Auth result provides subject_id for agent identity
    3. Scheduler router constructs service call with correct agent_id
    """
    http_client, raw_key = db_auth_client
    headers = {"Authorization": f"Bearer {raw_key}"}

    # 1. Submit
    resp = await http_client.post(
        "/api/v2/scheduler/submit",
        json={"executor": "worker", "task_type": "db_lifecycle", "priority": "high"},
        headers=headers,
    )
    assert resp.status_code == 201
    task_id = resp.json()["id"]
    assert resp.json()["priority_tier"] == "high"

    # 2. Get status
    resp = await http_client.get(
        f"/api/v2/scheduler/task/{task_id}",
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"

    # 3. Cancel
    resp = await http_client.post(
        f"/api/v2/scheduler/task/{task_id}/cancel",
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["cancelled"] is True


# =============================================================================
# 5. Performance Validation
# =============================================================================


@pytest.mark.asyncio
async def test_submit_latency_under_threshold(client: AsyncClient) -> None:
    """Single submit request completes under 200ms."""
    start = time.monotonic()
    resp = await client.post(
        "/api/v2/scheduler/submit",
        json={"executor": "worker", "task_type": "perf_single"},
    )
    elapsed_ms = (time.monotonic() - start) * 1000

    assert resp.status_code == 201
    assert elapsed_ms < 200, f"Submit took {elapsed_ms:.0f}ms, expected < 200ms"


@pytest.mark.asyncio
async def test_bulk_submit_throughput(client: AsyncClient) -> None:
    """50 sequential submits complete under 10s (< 200ms average per task)."""
    task_count = 50
    start = time.monotonic()

    for i in range(task_count):
        resp = await client.post(
            "/api/v2/scheduler/submit",
            json={
                "executor": f"worker-{i}",
                "task_type": "perf_bulk",
                "priority": "normal",
            },
        )
        assert resp.status_code == 201

    elapsed_s = time.monotonic() - start
    avg_ms = (elapsed_s / task_count) * 1000

    assert elapsed_s < 10, f"Bulk submit took {elapsed_s:.1f}s, expected < 10s"
    assert avg_ms < 200, f"Average {avg_ms:.0f}ms/task, expected < 200ms"
