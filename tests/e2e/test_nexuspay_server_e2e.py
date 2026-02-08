"""E2E tests for NexusPay SDK with real FastAPI server + permissions.

Tests NexusPay through the full create_app() server stack:
- Real create_app() with auth dependency override
- AsyncNexusFS with enforce_permissions=True
- ReBAC permission checks (mock allows/denies)
- NexusPay endpoints injected into real server
- x402 middleware for payment-gated file access
- Full lifecycle: auth → permission check → payment → access

Issue #1207: Unified NexusPay SDK
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncGenerator
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import Header, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nexus.core.async_nexus_fs import AsyncNexusFS
from nexus.core.async_permissions import AsyncPermissionEnforcer
from nexus.pay.credits import CreditsService
from nexus.pay.sdk import NexusPay
from nexus.pay.x402 import X402Client, X402PaymentVerification
from nexus.server.middleware.x402 import X402PaymentMiddleware
from nexus.storage.models import (
    DirectoryEntryModel,
    FilePathModel,
    VersionHistoryModel,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine, None]:
    """Create async SQLite engine for tests."""
    db_file = tmp_path / "nexuspay_perm_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", echo=False)

    async with engine.begin() as conn:
        for table in [
            FilePathModel.__table__,
            DirectoryEntryModel.__table__,
            VersionHistoryModel.__table__,
        ]:
            await conn.run_sync(lambda c, t=table: t.create(c, checkfirst=True))

    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def mock_rebac_manager() -> AsyncMock:
    """ReBAC manager that allows all by default."""
    mock = AsyncMock()
    mock.rebac_check.return_value = True

    async def allow_all_bulk(checks: list, **kwargs: Any) -> dict:
        return dict.fromkeys(checks, True)

    mock.rebac_check_bulk.side_effect = allow_all_bulk
    return mock


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
    mock_rebac_manager: AsyncMock,
    mock_credits_service: AsyncMock,
    x402_client: X402Client,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[AsyncClient, None]:
    """Full create_app() server with auth, permissions, and NexusPay.

    Replicates the production server stack:
    1. create_app() with real routes
    2. AsyncNexusFS with enforce_permissions=True
    3. Auth dependency override (authenticated user)
    4. NexusPay wired into app state
    5. x402 middleware for payment-gated paths
    """
    monkeypatch.setenv("NEXUS_SEARCH_DAEMON", "false")
    monkeypatch.setenv("NEXUS_ENFORCE_PERMISSIONS", "true")

    # Real AsyncNexusFS with permissions
    permission_enforcer = AsyncPermissionEnforcer(rebac_manager=mock_rebac_manager)
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

    # Create real app via create_app
    from nexus.server.fastapi_server import _app_state, create_app, get_auth_result

    db_file = tmp_path / "nexuspay_perm_test.db"
    app = create_app(
        nexus_fs=mock_nexus_fs,
        database_url=f"sqlite:///{db_file}",
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

    # Add NexusPay API endpoints to the real server
    @app.get("/api/v2/pay/balance")
    async def pay_balance(request: Request):
        pay: NexusPay = request.app.state.nexuspay
        balance = await pay.get_balance()
        return {
            "available": str(balance.available),
            "reserved": str(balance.reserved),
            "total": str(balance.total),
        }

    @app.post("/api/v2/pay/transfer")
    async def pay_transfer(request: Request):
        body = await request.json()
        pay: NexusPay = request.app.state.nexuspay
        receipt = await pay.transfer(
            to=body["to"],
            amount=body["amount"],
            memo=body.get("memo", ""),
        )
        return {
            "id": receipt.id,
            "method": receipt.method,
            "amount": str(receipt.amount),
        }

    @app.get("/api/v2/pay/premium")
    async def premium_content():
        return {"data": "premium content", "paid": True}

    @app.post("/api/v2/pay/reserve")
    async def pay_reserve(request: Request):
        body = await request.json()
        pay: NexusPay = request.app.state.nexuspay
        reservation = await pay.reserve(amount=body["amount"])
        return {"id": reservation.id, "status": reservation.status}

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
    """NexusPay transfer works through real server stack."""
    resp = await client.post(
        "/api/v2/pay/transfer",
        json={"to": "agent-bob", "amount": 5.0, "memo": "server test"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["method"] == "credits"
    assert data["amount"] == "5.0"
    mock_credits_service.transfer.assert_called_once()


@pytest.mark.asyncio
async def test_reserve_through_server(
    client: AsyncClient,
    mock_credits_service: AsyncMock,
) -> None:
    """NexusPay reservation works through real server stack."""
    resp = await client.post(
        "/api/v2/pay/reserve",
        json={"amount": 10.0},
    )
    assert resp.status_code == 200
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

    payment = base64.b64encode(
        json.dumps({"tx_hash": "0x" + "ab" * 32}).encode()
    ).decode()

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
        return X402PaymentVerification(
            valid=False, tx_hash=None, amount=None, error="Bad sig"
        )

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
async def test_file_write_denied_when_permission_revoked(
    client: AsyncClient,
    mock_rebac_manager: AsyncMock,
) -> None:
    """File write fails with 403 when ReBAC denies permission."""
    mock_rebac_manager.rebac_check.return_value = False

    resp = await client.post(
        "/api/v2/files/write",
        json={"path": "/denied/file.txt", "content": "nope"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_payment_and_file_ops_coexist(
    client: AsyncClient,
    mock_credits_service: AsyncMock,
) -> None:
    """NexusPay and file operations work in the same server."""
    # Pay for something
    resp = await client.post(
        "/api/v2/pay/transfer",
        json={"to": "agent-worker", "amount": 3.0, "memo": "File access"},
    )
    assert resp.status_code == 200
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

    # 2. Make internal transfer (uses CreditsService)
    resp = await client.post(
        "/api/v2/pay/transfer",
        json={"to": "worker", "amount": 5.0, "memo": "Task bounty"},
    )
    assert resp.status_code == 200

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
    payment = base64.b64encode(
        json.dumps({"tx_hash": "0x" + "ff" * 32}).encode()
    ).decode()

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
