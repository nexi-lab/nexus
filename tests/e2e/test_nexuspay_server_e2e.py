"""E2E tests for NexusPay SDK with real FastAPI server + permissions + PostgreSQL.

Tests NexusPay through the full create_app() server stack:
- Real create_app() with auth dependency override
- PostgreSQL database (requires Docker container on localhost:5432)
- AsyncNexusFS with enforce_permissions=True
- Real AsyncReBACManager for permission checks (PostgreSQL-backed)
- DatabaseAPIKeyAuth for auth_type=database tests
- NexusPay endpoints injected into real server
- x402 middleware for payment-gated file access
- Full lifecycle: auth → ReBAC permission check → payment → access
- Pay endpoint auth enforcement tests

Issue #1207: Unified NexusPay SDK
Issue #1209: Add Nexus Pay REST API endpoints

Requirements:
- PostgreSQL running on localhost:5432 (use docker-compose or local install)
- Database: scorpio (user: scorpio, password: scorpio) or set TEST_DATABASE_URL
"""

from __future__ import annotations

import base64
import json
import os
import socket
from collections.abc import AsyncGenerator
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import Header
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nexus.core.async_nexus_fs import AsyncNexusFS
from nexus.core.async_permissions import AsyncPermissionEnforcer
from nexus.core.async_rebac_manager import AsyncReBACManager
from nexus.pay.credits import CreditsService
from nexus.pay.sdk import NexusPay
from nexus.pay.x402 import X402Client, X402PaymentVerification
from nexus.server.middleware.x402 import X402PaymentMiddleware
from nexus.storage.models import (
    DirectoryEntryModel,
    FilePathModel,
    VersionHistoryModel,
)


def _pg_available() -> bool:
    """Check if PostgreSQL is reachable at localhost:5432."""
    try:
        with socket.create_connection(("localhost", 5432), timeout=1):
            return True
    except OSError:
        return False


PG_AVAILABLE = _pg_available()

# PostgreSQL test database URL (async driver for engine fixture)
# Default: connect to local scorpio-postgres container
# Override with TEST_DATABASE_URL environment variable
TEST_ASYNC_DB_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql+asyncpg://scorpio:scorpio@localhost:5432/scorpio"
)

# Sync URL for create_app() (which converts internally for async operations)
TEST_SYNC_DB_URL = TEST_ASYNC_DB_URL.replace("+asyncpg", "").replace("+aiosqlite", "")

# Skip all tests if PostgreSQL is not available (e.g., in CI without Docker)
pytestmark = [
    pytest.mark.xdist_group("nexuspay_server_e2e"),
    pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL not available at localhost:5432"),
]

# =============================================================================
# Fixtures
# =============================================================================


@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    """Create async PostgreSQL engine for tests.

    Uses the scorpio-postgres Docker container by default.
    Creates file storage tables + ReBAC tables (tuples, namespaces, group_closure).
    Cleaned via TRUNCATE on setup and teardown.
    """
    engine = create_async_engine(TEST_ASYNC_DB_URL, echo=False)

    async with engine.begin() as conn:
        # File storage tables (ORM models)
        tables = [
            FilePathModel.__table__,
            DirectoryEntryModel.__table__,
            VersionHistoryModel.__table__,
        ]
        for table in tables:
            await conn.run_sync(lambda c, t=table: t.create(c, checkfirst=True))

        # ReBAC tables (raw SQL for compatibility with async_rebac_manager queries)
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS rebac_tuples (
                tuple_id TEXT PRIMARY KEY,
                subject_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                subject_relation TEXT,
                relation TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                zone_id TEXT NOT NULL DEFAULT 'default',
                conditions TEXT,
                expires_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS rebac_namespaces (
                namespace_id TEXT PRIMARY KEY,
                object_type TEXT NOT NULL UNIQUE,
                config TEXT NOT NULL,
                created_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS rebac_group_closure (
                member_type TEXT NOT NULL,
                member_id TEXT NOT NULL,
                group_type TEXT NOT NULL,
                group_id TEXT NOT NULL,
                zone_id TEXT NOT NULL DEFAULT 'default',
                depth INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMPTZ,
                PRIMARY KEY (member_type, member_id, group_type, group_id, zone_id)
            )
        """))

        # Clean any leftover test data
        try:
            await conn.execute(text(
                "TRUNCATE file_paths, directory_entries, version_history, "
                "rebac_tuples, rebac_namespaces, rebac_group_closure CASCADE"
            ))
        except Exception:
            pass

        # Insert default namespace config: file type with owner/writer/reader → read/write
        ns_config = json.dumps({
            "relations": {"owner": {}, "writer": {}, "reader": {}, "direct_owner": {}},
            "permissions": {
                "read": {"union": ["owner", "writer", "reader", "direct_owner"]},
                "write": {"union": ["owner", "writer", "direct_owner"]},
                "admin": {"union": ["owner"]},
            },
        })
        await conn.execute(
            text(
                "INSERT INTO rebac_namespaces (namespace_id, object_type, config) "
                "VALUES (:id, :type, :config) "
                "ON CONFLICT (object_type) DO UPDATE SET config = :config"
            ),
            {"id": "file_ns", "type": "file", "config": ns_config},
        )

    yield engine

    # Cleanup after tests
    async with engine.begin() as conn:
        try:
            await conn.execute(text(
                "TRUNCATE file_paths, directory_entries, version_history, "
                "rebac_tuples, rebac_namespaces, rebac_group_closure CASCADE"
            ))
        except Exception:
            pass

    await engine.dispose()


@pytest_asyncio.fixture
async def rebac_manager(engine: AsyncEngine) -> AsyncReBACManager:
    """Real AsyncReBACManager backed by PostgreSQL.

    Creates a real ReBAC manager with L1 cache disabled (for test isolation).
    Grants test_user owner permission on root "/" in zone test-tenant,
    so file operations succeed for paths under /.
    """
    manager = AsyncReBACManager(engine, enable_l1_cache=False)

    # Grant test_user owner on root "/" — inherits to all child paths
    await manager.write_tuple(
        subject=("user", "test_user"),
        relation="owner",
        object=("file", "/"),
        zone_id="test-tenant",
    )

    return manager


@pytest.fixture
def mock_credits_service():
    """Mock CreditsService for NexusPay."""
    service = AsyncMock(spec=CreditsService)
    service.get_balance.return_value = Decimal("100.0")
    service.get_balance_with_reserved.return_value = (Decimal("100.0"), Decimal("5.0"))
    service.check_budget.return_value = True
    service.transfer.return_value = "tx-server-001"
    service.reserve.return_value = "res-server-001"
    service.commit_reservation.return_value = None
    service.release_reservation.return_value = None
    service.deduct_fast.return_value = True
    service.transfer_batch.return_value = ["tx-b1", "tx-b2"]
    service.provision_wallet.return_value = None
    service.topup.return_value = "topup-server-001"
    return service


@pytest.fixture
def x402_client():
    """Real X402Client for payment-gated tests."""
    return X402Client(
        facilitator_url="https://x402.org/facilitator",
        wallet_address="0x1234567890123456789012345678901234567890",
        network="base",
        webhook_secret="test-secret",
    )


@pytest_asyncio.fixture
async def client(
    tmp_path: Path,
    engine: AsyncEngine,
    rebac_manager: AsyncReBACManager,
    mock_credits_service: AsyncMock,
    x402_client: X402Client,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[AsyncClient, None]:
    """Full create_app() server with auth, permissions, PostgreSQL, and NexusPay.

    Replicates the production server stack:
    1. create_app() with real routes and PostgreSQL
    2. AsyncNexusFS with enforce_permissions=True (PostgreSQL-backed)
    3. Real AsyncReBACManager for permission checks (PostgreSQL-backed)
    4. Auth dependency override (authenticated user)
    5. NexusPay wired into app state
    6. x402 middleware for payment-gated paths
    """
    monkeypatch.setenv("NEXUS_SEARCH_DAEMON", "false")
    monkeypatch.setenv("NEXUS_ENFORCE_PERMISSIONS", "true")

    # Real AsyncNexusFS with real ReBAC permissions (PostgreSQL engine)
    permission_enforcer = AsyncPermissionEnforcer(rebac_manager=rebac_manager)
    async_fs = AsyncNexusFS(
        backend_root=tmp_path / "backend",
        engine=engine,
        tenant_id="test-tenant",
        enforce_permissions=True,
        permission_enforcer=permission_enforcer,
    )
    await async_fs.initialize()

    # Minimal mock for sync NexusFS (required by create_app signature)
    mock_nexus_fs = MagicMock()
    mock_nexus_fs._event_bus = None
    mock_nexus_fs._coordination_client = None

    # Create real app via create_app with PostgreSQL URL
    from nexus.server.fastapi_server import _app_state, create_app, get_auth_result

    app = create_app(
        nexus_fs=mock_nexus_fs,
        database_url=TEST_SYNC_DB_URL,
    )

    # Inject AsyncNexusFS (simulating lifespan)
    _app_state.async_nexus_fs = async_fs

    # Wire NexusPay into the server
    nexuspay = NexusPay(
        api_key="nx_live_testuser",
        credits_service=mock_credits_service,
        x402_client=x402_client,
    )
    app.state.nexuspay = nexuspay
    app.state.x402_client = x402_client
    app.state.credits_service = mock_credits_service

    # Add x402 middleware for payment-gated file reads
    app.add_middleware(
        X402PaymentMiddleware,
        x402_client=x402_client,
        protected_paths={
            "/api/v2/pay/premium": Decimal("1.00"),
        },
    )

    # Pay router is now registered by create_app() via fastapi_server.py (Issue #1209).
    # Only add ad-hoc endpoints that don't overlap with the router.
    @app.get("/api/v2/pay/premium")
    async def premium_content():
        return {"data": "premium content", "paid": True}

    # Override auth to return an authenticated non-admin user.
    # Must match the exact signature of get_auth_result (FastAPI Header params).
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

    await async_fs.close()
    _app_state.async_nexus_fs = None
    app.dependency_overrides.clear()


# =============================================================================
# 1. NexusPay through real server stack (auth + permissions enabled)
# =============================================================================


@pytest.mark.asyncio
async def test_health_with_permissions(client: AsyncClient) -> None:
    """Health check works on real server with permissions enabled."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"


@pytest.mark.asyncio
async def test_balance_through_server(client: AsyncClient) -> None:
    """NexusPay balance works through real server stack."""
    resp = await client.get("/api/v2/pay/balance")
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] == "100.0"
    assert data["reserved"] == "5.0"
    assert data["total"] == "105.0"


@pytest.mark.asyncio
async def test_transfer_through_server(
    client: AsyncClient,
    mock_credits_service: AsyncMock,
) -> None:
    """NexusPay transfer works through real server stack (Issue #1209 router)."""
    resp = await client.post(
        "/api/v2/pay/transfer",
        json={"to": "agent-bob", "amount": "5.00", "memo": "server test"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["method"] == "credits"
    assert data["amount"] == "5.00"
    mock_credits_service.transfer.assert_called_once()


@pytest.mark.asyncio
async def test_reserve_through_server(
    client: AsyncClient,
    mock_credits_service: AsyncMock,
) -> None:
    """NexusPay reservation works through real server stack (Issue #1209 router)."""
    resp = await client.post(
        "/api/v2/pay/reserve",
        json={"amount": "10.00"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "pending"
    mock_credits_service.reserve.assert_called_once()


# =============================================================================
# 2. x402 payment-gated endpoints on real server
# =============================================================================


@pytest.mark.asyncio
async def test_premium_returns_402_on_real_server(client: AsyncClient) -> None:
    """Premium endpoint returns 402 on real server without payment."""
    resp = await client.get("/api/v2/pay/premium")
    assert resp.status_code == 402
    assert "X-Payment-Required" in resp.headers

    payload = json.loads(base64.b64decode(resp.headers["X-Payment-Required"]).decode())
    assert payload["amount"] == "1.00"
    assert payload["currency"] == "USDC"


@pytest.mark.asyncio
async def test_premium_unlocks_with_payment_on_real_server(
    client: AsyncClient,
    x402_client: X402Client,
) -> None:
    """Premium endpoint unlocks with valid x402 payment on real server."""

    async def mock_verify(payment_header, expected_amount):
        return X402PaymentVerification(
            valid=True,
            tx_hash="0x" + "ab" * 32,
            amount=expected_amount,
            error=None,
        )

    x402_client.verify_payment = mock_verify

    payment = base64.b64encode(json.dumps({"tx_hash": "0x" + "ab" * 32}).encode()).decode()

    resp = await client.get(
        "/api/v2/pay/premium",
        headers={"X-Payment": payment},
    )
    assert resp.status_code == 200
    assert resp.json()["paid"] is True


@pytest.mark.asyncio
async def test_premium_rejects_invalid_payment_on_real_server(
    client: AsyncClient,
    x402_client: X402Client,
) -> None:
    """Invalid payment rejected on real server."""

    async def mock_verify(payment_header, expected_amount):
        return X402PaymentVerification(valid=False, tx_hash=None, amount=None, error="Bad sig")

    x402_client.verify_payment = mock_verify

    payment = base64.b64encode(b'{"bad": "pay"}').decode()
    resp = await client.get(
        "/api/v2/pay/premium",
        headers={"X-Payment": payment},
    )
    assert resp.status_code == 402


# =============================================================================
# 3. File operations with permissions + payment in same server
# =============================================================================


@pytest.mark.asyncio
async def test_file_write_with_permissions_enabled(client: AsyncClient) -> None:
    """File write works when user has permission (ReBAC allows)."""
    resp = await client.post(
        "/api/v2/files/write",
        json={"path": "/pay/test.txt", "content": "payment test"},
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == 1


@pytest.mark.asyncio
async def test_file_write_denied_when_no_permission(
    tmp_path: Path,
    engine: AsyncEngine,
    rebac_manager: AsyncReBACManager,
    mock_credits_service: AsyncMock,
    x402_client: X402Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """File write fails with 403 when user has no ReBAC tuple for the path.

    Uses a separate client with a user who has NO owner tuple on root "/",
    so the real AsyncReBACManager returns False for all file permission checks.
    """
    monkeypatch.setenv("NEXUS_SEARCH_DAEMON", "false")
    monkeypatch.setenv("NEXUS_ENFORCE_PERMISSIONS", "true")

    # No permission tuples for "denied_user" — real ReBAC will deny
    permission_enforcer = AsyncPermissionEnforcer(rebac_manager=rebac_manager)
    async_fs = AsyncNexusFS(
        backend_root=tmp_path / "backend_denied",
        engine=engine,
        tenant_id="test-tenant",
        enforce_permissions=True,
        permission_enforcer=permission_enforcer,
    )
    await async_fs.initialize()

    mock_nexus_fs = MagicMock()
    mock_nexus_fs._event_bus = None
    mock_nexus_fs._coordination_client = None

    from nexus.server.fastapi_server import _app_state, create_app, get_auth_result

    app = create_app(nexus_fs=mock_nexus_fs, database_url=TEST_SYNC_DB_URL)
    _app_state.async_nexus_fs = async_fs
    app.state.credits_service = mock_credits_service
    app.state.x402_client = x402_client

    # Auth as "denied_user" who has no ReBAC tuples
    async def denied_auth(
        authorization: str | None = Header(None, alias="Authorization"),
        x_agent_id: str | None = Header(None, alias="X-Agent-ID"),
        x_nexus_subject: str | None = Header(None, alias="X-Nexus-Subject"),
        x_nexus_zone_id: str | None = Header(None, alias="X-Nexus-Zone-ID"),
    ) -> dict[str, Any]:
        return {
            "authenticated": True,
            "subject_type": "user",
            "subject_id": "denied_user",
            "zone_id": "test-tenant",
            "is_admin": False,
        }

    app.dependency_overrides[get_auth_result] = denied_auth

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as denied_client:
        resp = await denied_client.post(
            "/api/v2/files/write",
            json={"path": "/denied/file.txt", "content": "nope"},
        )
        assert resp.status_code == 403

    await async_fs.close()
    _app_state.async_nexus_fs = None
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_payment_and_file_ops_coexist(
    client: AsyncClient,
    mock_credits_service: AsyncMock,
) -> None:
    """NexusPay and file operations work in the same server."""
    # Pay for something (using #1209 router - string amounts, 201 status)
    resp = await client.post(
        "/api/v2/pay/transfer",
        json={"to": "agent-worker", "amount": "3.00", "memo": "File access"},
    )
    assert resp.status_code == 201
    assert resp.json()["method"] == "credits"

    # Then write a file (with permissions)
    resp = await client.post(
        "/api/v2/files/write",
        json={"path": "/pay/after-payment.txt", "content": "paid content"},
    )
    assert resp.status_code == 200

    # Then read it back
    resp = await client.get("/api/v2/files/read", params={"path": "/pay/after-payment.txt"})
    assert resp.status_code == 200
    assert resp.json()["content"] == "paid content"


@pytest.mark.asyncio
async def test_full_lifecycle_auth_permission_payment(
    client: AsyncClient,
    mock_credits_service: AsyncMock,
    x402_client: X402Client,
) -> None:
    """Full lifecycle: auth → permission → NexusPay transfer → x402 payment → file write.

    Verifies all layers work together in the real server.
    """
    # 1. Check balance (auth required, passes via override)
    resp = await client.get("/api/v2/pay/balance")
    assert resp.status_code == 200
    assert Decimal(resp.json()["available"]) > 0

    # 2. Make internal transfer (uses #1209 router - string amounts, 201 status)
    resp = await client.post(
        "/api/v2/pay/transfer",
        json={"to": "worker", "amount": "5.00", "memo": "Task bounty"},
    )
    assert resp.status_code == 201

    # 3. Try premium endpoint without payment → 402
    resp = await client.get("/api/v2/pay/premium")
    assert resp.status_code == 402

    # 4. Pay and access premium endpoint
    async def mock_verify(payment_header, expected_amount):
        return X402PaymentVerification(
            valid=True,
            tx_hash="0x" + "ff" * 32,
            amount=expected_amount,
            error=None,
        )

    x402_client.verify_payment = mock_verify
    payment = base64.b64encode(json.dumps({"tx_hash": "0x" + "ff" * 32}).encode()).decode()

    resp = await client.get(
        "/api/v2/pay/premium",
        headers={"X-Payment": payment},
    )
    assert resp.status_code == 200
    assert resp.json()["paid"] is True

    # 5. Write a file (ReBAC allows)
    resp = await client.post(
        "/api/v2/files/write",
        json={"path": "/lifecycle/result.txt", "content": "lifecycle complete"},
    )
    assert resp.status_code == 200


# =============================================================================
# 4. Pay endpoint auth enforcement with permissions enabled
# =============================================================================


@pytest_asyncio.fixture
async def auth_enforced_client(
    tmp_path: Path,
    engine: AsyncEngine,
    rebac_manager: AsyncReBACManager,
    mock_credits_service: AsyncMock,
    x402_client: X402Client,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[AsyncClient, None]:
    """Server with api_key auth enforced (no open-access mode).

    Unlike the main `client` fixture which overrides auth, this fixture
    configures a real api_key so unauthenticated requests are rejected with 401.
    """
    monkeypatch.setenv("NEXUS_SEARCH_DAEMON", "false")
    monkeypatch.setenv("NEXUS_ENFORCE_PERMISSIONS", "true")

    permission_enforcer = AsyncPermissionEnforcer(rebac_manager=rebac_manager)
    async_fs = AsyncNexusFS(
        backend_root=tmp_path / "backend",
        engine=engine,
        tenant_id="test-tenant",
        enforce_permissions=True,
        permission_enforcer=permission_enforcer,
    )
    await async_fs.initialize()

    mock_nexus_fs = MagicMock()
    mock_nexus_fs._event_bus = None
    mock_nexus_fs._coordination_client = None

    from nexus.server.fastapi_server import _app_state, create_app

    app = create_app(
        nexus_fs=mock_nexus_fs,
        database_url=TEST_SYNC_DB_URL,
        api_key="test-secret-api-key-12345",
    )
    _app_state.async_nexus_fs = async_fs
    app.state.credits_service = mock_credits_service
    app.state.x402_client = x402_client

    # NO auth override → real auth chain enforces api_key

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as http_client:
        yield http_client

    await async_fs.close()
    _app_state.async_nexus_fs = None


@pytest.mark.asyncio
async def test_pay_balance_requires_auth(auth_enforced_client: AsyncClient) -> None:
    """Pay balance endpoint returns 401 without valid API key."""
    resp = await auth_enforced_client.get("/api/v2/pay/balance")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_pay_transfer_requires_auth(auth_enforced_client: AsyncClient) -> None:
    """Pay transfer endpoint returns 401 without valid API key."""
    resp = await auth_enforced_client.post(
        "/api/v2/pay/transfer",
        json={"to": "agent-bob", "amount": "5.00", "memo": "should fail"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_pay_reserve_requires_auth(auth_enforced_client: AsyncClient) -> None:
    """Pay reserve endpoint returns 401 without valid API key."""
    resp = await auth_enforced_client.post(
        "/api/v2/pay/reserve",
        json={"amount": "10.00"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_pay_succeeds_with_valid_api_key(
    auth_enforced_client: AsyncClient,
    mock_credits_service: AsyncMock,
) -> None:
    """Pay balance works when valid API key is provided in Authorization header."""
    resp = await auth_enforced_client.get(
        "/api/v2/pay/balance",
        headers={"Authorization": "Bearer test-secret-api-key-12345"},
    )
    assert resp.status_code == 200
    assert resp.json()["available"] == "100.0"


@pytest.mark.asyncio
async def test_pay_operations_scoped_to_authenticated_agent(
    client: AsyncClient,
    mock_credits_service: AsyncMock,
) -> None:
    """Pay operations use the authenticated agent's identity (subject_id = test_user).

    Verifies that NexusPay SDK is constructed with the correct agent_id from auth,
    ensuring agent isolation in a multi-tenant environment.
    """
    # Transfer should use the authenticated agent's identity
    resp = await client.post(
        "/api/v2/pay/transfer",
        json={"to": "agent-worker", "amount": "2.00", "memo": "scoped test"},
    )
    assert resp.status_code == 201
    data = resp.json()
    # The from_agent should be the authenticated user (test_user from mock_auth_result)
    assert data["from_agent"] == "test_user"


@pytest.mark.asyncio
async def test_file_permission_denied_does_not_affect_pay(
    tmp_path: Path,
    engine: AsyncEngine,
    rebac_manager: AsyncReBACManager,
    mock_credits_service: AsyncMock,
    x402_client: X402Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ReBAC denies file permissions, pay endpoints still work.

    Pay endpoints use auth (require_auth) not ReBAC for authorization.
    File endpoints use ReBAC. These should be independent.
    Uses a user with no ReBAC tuples so file writes are denied.
    """
    monkeypatch.setenv("NEXUS_SEARCH_DAEMON", "false")
    monkeypatch.setenv("NEXUS_ENFORCE_PERMISSIONS", "true")

    permission_enforcer = AsyncPermissionEnforcer(rebac_manager=rebac_manager)
    async_fs = AsyncNexusFS(
        backend_root=tmp_path / "backend_payvsdeny",
        engine=engine,
        tenant_id="test-tenant",
        enforce_permissions=True,
        permission_enforcer=permission_enforcer,
    )
    await async_fs.initialize()

    mock_nexus_fs = MagicMock()
    mock_nexus_fs._event_bus = None
    mock_nexus_fs._coordination_client = None

    from nexus.server.fastapi_server import _app_state, create_app, get_auth_result

    app = create_app(nexus_fs=mock_nexus_fs, database_url=TEST_SYNC_DB_URL)
    _app_state.async_nexus_fs = async_fs
    app.state.credits_service = mock_credits_service
    app.state.x402_client = x402_client

    # Auth as "noperm_user" who has no file ReBAC tuples
    async def noperm_auth(
        authorization: str | None = Header(None, alias="Authorization"),
        x_agent_id: str | None = Header(None, alias="X-Agent-ID"),
        x_nexus_subject: str | None = Header(None, alias="X-Nexus-Subject"),
        x_nexus_zone_id: str | None = Header(None, alias="X-Nexus-Zone-ID"),
    ) -> dict[str, Any]:
        return {
            "authenticated": True,
            "subject_type": "user",
            "subject_id": "noperm_user",
            "zone_id": "test-tenant",
            "is_admin": False,
        }

    app.dependency_overrides[get_auth_result] = noperm_auth

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as noperm_client:
        # File write should fail with 403 (real ReBAC: no tuple for noperm_user)
        resp = await noperm_client.post(
            "/api/v2/files/write",
            json={"path": "/denied/file.txt", "content": "nope"},
        )
        assert resp.status_code == 403

        # Pay balance should still work (not subject to ReBAC)
        resp = await noperm_client.get("/api/v2/pay/balance")
        assert resp.status_code == 200
        assert resp.json()["available"] == "100.0"

        # Pay transfer should still work
        resp = await noperm_client.post(
            "/api/v2/pay/transfer",
            json={"to": "agent-bob", "amount": "1.00", "memo": "works despite file deny"},
        )
        assert resp.status_code == 201

    await async_fs.close()
    _app_state.async_nexus_fs = None
    app.dependency_overrides.clear()


# =============================================================================
# 5. DatabaseAPIKeyAuth (auth_type=database) with PostgreSQL
# =============================================================================


@pytest_asyncio.fixture
async def db_auth_client(
    tmp_path: Path,
    engine: AsyncEngine,
    rebac_manager: AsyncReBACManager,
    mock_credits_service: AsyncMock,
    x402_client: X402Client,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[tuple[AsyncClient, str], None]:
    """Server with DatabaseAPIKeyAuth (auth_type=database) backed by PostgreSQL.

    Creates a real DatabaseAPIKeyAuth provider, provisions a test API key
    in PostgreSQL, and returns the client + raw API key for Bearer auth.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from nexus.server.auth.database_key import DatabaseAPIKeyAuth
    from nexus.storage.models import APIKeyModel

    monkeypatch.setenv("NEXUS_SEARCH_DAEMON", "false")
    monkeypatch.setenv("NEXUS_ENFORCE_PERMISSIONS", "true")

    # Sync engine + session factory for DatabaseAPIKeyAuth
    sync_engine = create_engine(TEST_SYNC_DB_URL, echo=False)
    SessionFactory = sessionmaker(bind=sync_engine)

    # Ensure api_keys table exists
    with sync_engine.begin() as conn:
        APIKeyModel.__table__.create(conn, checkfirst=True)

    # Create a test API key in PostgreSQL
    with SessionFactory() as session:
        key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="pay_test_user",
            name="NexusPay E2E Test Key",
            subject_type="user",
            subject_id="pay_test_agent",
            zone_id="test-tenant",
            is_admin=False,
        )
        session.commit()

    # Real DatabaseAPIKeyAuth provider
    db_auth = DatabaseAPIKeyAuth(SessionFactory)

    # AsyncNexusFS with real ReBAC permissions
    permission_enforcer = AsyncPermissionEnforcer(rebac_manager=rebac_manager)
    async_fs = AsyncNexusFS(
        backend_root=tmp_path / "backend",
        engine=engine,
        tenant_id="test-tenant",
        enforce_permissions=True,
        permission_enforcer=permission_enforcer,
    )
    await async_fs.initialize()

    mock_nexus_fs = MagicMock()
    mock_nexus_fs._event_bus = None
    mock_nexus_fs._coordination_client = None

    from nexus.server.fastapi_server import _app_state, create_app

    app = create_app(
        nexus_fs=mock_nexus_fs,
        database_url=TEST_SYNC_DB_URL,
        auth_provider=db_auth,
    )
    _app_state.async_nexus_fs = async_fs
    app.state.credits_service = mock_credits_service
    app.state.x402_client = x402_client

    # NO auth override — real DatabaseAPIKeyAuth validates tokens

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as http_client:
        yield (http_client, raw_key)

    await async_fs.close()
    _app_state.async_nexus_fs = None

    # Cleanup: remove test key from PostgreSQL
    with SessionFactory() as session:
        session.execute(
            text("DELETE FROM api_keys WHERE key_id = :key_id"),
            {"key_id": key_id},
        )
        session.commit()

    sync_engine.dispose()


@pytest.mark.asyncio
async def test_db_auth_pay_balance_no_token(
    db_auth_client: tuple[AsyncClient, str],
) -> None:
    """Pay balance returns 401 without Bearer token (DatabaseAPIKeyAuth)."""
    http_client, _ = db_auth_client
    resp = await http_client.get("/api/v2/pay/balance")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_db_auth_pay_balance_invalid_token(
    db_auth_client: tuple[AsyncClient, str],
) -> None:
    """Pay balance returns 401 with invalid token (DatabaseAPIKeyAuth)."""
    http_client, _ = db_auth_client
    resp = await http_client.get(
        "/api/v2/pay/balance",
        headers={"Authorization": "Bearer invalid-garbage-token"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_db_auth_pay_balance_valid_key(
    db_auth_client: tuple[AsyncClient, str],
) -> None:
    """Pay balance succeeds with valid database-backed API key."""
    http_client, raw_key = db_auth_client
    resp = await http_client.get(
        "/api/v2/pay/balance",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] == "100.0"
    assert data["reserved"] == "5.0"


@pytest.mark.asyncio
async def test_db_auth_pay_transfer_valid_key(
    db_auth_client: tuple[AsyncClient, str],
    mock_credits_service: AsyncMock,
) -> None:
    """Pay transfer succeeds with valid database-backed API key and correct agent identity."""
    http_client, raw_key = db_auth_client
    resp = await http_client.post(
        "/api/v2/pay/transfer",
        json={"to": "agent-bob", "amount": "3.00", "memo": "db auth test"},
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["method"] == "credits"
    assert data["amount"] == "3.00"
    # Agent ID should come from the database key's subject_id
    assert data["from_agent"] == "pay_test_agent"
    mock_credits_service.transfer.assert_called_once()


@pytest.mark.asyncio
async def test_db_auth_pay_transfer_no_token(
    db_auth_client: tuple[AsyncClient, str],
) -> None:
    """Pay transfer returns 401 without Bearer token (DatabaseAPIKeyAuth)."""
    http_client, _ = db_auth_client
    resp = await http_client.post(
        "/api/v2/pay/transfer",
        json={"to": "agent-bob", "amount": "5.00", "memo": "should fail"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_db_auth_full_lifecycle(
    db_auth_client: tuple[AsyncClient, str],
    mock_credits_service: AsyncMock,
) -> None:
    """Full pay lifecycle with DatabaseAPIKeyAuth: balance → transfer → reserve.

    Validates the complete auth_type=database flow through the real server:
    1. DatabaseAPIKeyAuth looks up sk- key in PostgreSQL
    2. Auth result provides subject_id for agent identity
    3. Pay router constructs NexusPay with correct agent_id
    4. All pay operations scoped to the authenticated agent
    """
    http_client, raw_key = db_auth_client
    headers = {"Authorization": f"Bearer {raw_key}"}

    # 1. Check balance
    resp = await http_client.get("/api/v2/pay/balance", headers=headers)
    assert resp.status_code == 200
    assert Decimal(resp.json()["available"]) > 0

    # 2. Transfer
    resp = await http_client.post(
        "/api/v2/pay/transfer",
        json={"to": "worker-agent", "amount": "5.00", "memo": "db auth lifecycle"},
        headers=headers,
    )
    assert resp.status_code == 201
    assert resp.json()["from_agent"] == "pay_test_agent"

    # 3. Reserve
    resp = await http_client.post(
        "/api/v2/pay/reserve",
        json={"amount": "10.00"},
        headers=headers,
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "pending"

    # 4. Can afford check
    resp = await http_client.get(
        "/api/v2/pay/can-afford",
        params={"amount": "1.00"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["can_afford"] is True
