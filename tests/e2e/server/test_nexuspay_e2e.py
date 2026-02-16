"""End-to-end tests for NexusPay SDK with FastAPI server.

Tests the full flow: NexusPay SDK → CreditsService → x402 middleware → FastAPI,
including payment-gated endpoints and permission checks.

These tests build a standalone FastAPI app that exercises:
1. NexusPay SDK transfer + balance operations through a server
2. x402 middleware blocking/allowing requests based on payment
3. @metered decorator charging per API call
4. Budget context manager enforcing limits
5. NexusPay used as payment backend for protected endpoints
"""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from nexus.pay.credits import CreditsService
from nexus.pay.sdk import Balance, BudgetExceededError, NexusPay, NexusPayError, Receipt
from nexus.pay.x402 import X402Client, X402PaymentVerification

# We import middleware directly from its module to avoid the heavy
# nexus.server.__init__ import chain (which requires litellm etc.)
from nexus.server.middleware.x402 import X402PaymentMiddleware

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def credits_service():
    """Mock CreditsService that behaves like disabled mode."""
    service = AsyncMock(spec=CreditsService)
    service.get_balance = AsyncMock(return_value=Decimal("100.0"))
    service.get_balance_with_reserved = AsyncMock(return_value=(Decimal("100.0"), Decimal("5.0")))
    service.check_budget = AsyncMock(return_value=True)
    service.transfer = AsyncMock(return_value="tx-e2e-001")
    service.transfer_batch = AsyncMock(return_value=["tx-b1", "tx-b2"])
    service.reserve = AsyncMock(return_value="res-e2e-001")
    service.commit_reservation = AsyncMock()
    service.release_reservation = AsyncMock()
    service.deduct_fast = AsyncMock(return_value=True)
    service.provision_wallet = AsyncMock()
    service.topup = AsyncMock(return_value="topup-e2e-001")
    return service


@pytest.fixture
def x402_client():
    """Real X402Client with test wallet."""
    return X402Client(
        facilitator_url="https://x402.org/facilitator",
        wallet_address="0x1234567890123456789012345678901234567890",
        network="base",
        webhook_secret="test-secret",
    )


@pytest.fixture
def nexuspay(credits_service, x402_client):
    """NexusPay SDK instance wired to mock services."""
    return NexusPay(
        api_key="nx_live_e2e_agent",
        credits_service=credits_service,
        x402_client=x402_client,
    )


@pytest.fixture
def app(nexuspay, x402_client, credits_service):
    """FastAPI app with NexusPay-powered endpoints."""
    app = FastAPI(title="NexusPay E2E Test")

    # Store SDK + services in app state
    app.state.nexuspay = nexuspay
    app.state.x402_client = x402_client
    app.state.credits_service = credits_service

    # Add x402 middleware for payment-gated paths
    app.add_middleware(
        X402PaymentMiddleware,
        x402_client=x402_client,
        protected_paths={
            "/api/premium": Decimal("1.00"),
        },
    )

    # --- Public endpoint ---
    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    # --- Balance endpoint (uses NexusPay) ---
    @app.get("/api/balance")
    async def get_balance(request: Request):
        pay: NexusPay = request.app.state.nexuspay
        balance = await pay.get_balance()
        return {
            "available": str(balance.available),
            "reserved": str(balance.reserved),
            "total": str(balance.total),
        }

    # --- Transfer endpoint (uses NexusPay) ---
    @app.post("/api/transfer")
    async def do_transfer(request: Request):
        body = await request.json()
        pay: NexusPay = request.app.state.nexuspay
        try:
            receipt = await pay.transfer(
                to=body["to"],
                amount=body["amount"],
                memo=body.get("memo", ""),
            )
            return {
                "id": receipt.id,
                "method": receipt.method,
                "amount": str(receipt.amount),
                "to": receipt.to_agent,
            }
        except NexusPayError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    # --- Metered endpoint (charges per call) ---
    @app.get("/api/search")
    async def metered_search(request: Request, q: str = ""):
        pay: NexusPay = request.app.state.nexuspay
        success = await pay.meter(amount=Decimal("0.001"), event_type="search")
        if not success:
            raise HTTPException(status_code=402, detail="Insufficient credits")
        return {"query": q, "results": ["result1", "result2"], "charged": "0.001"}

    # --- Payment-gated premium endpoint (x402 middleware) ---
    @app.get("/api/premium")
    async def premium_content():
        return {"data": "premium content", "paid": True}

    # --- Reservation endpoint (two-phase) ---
    @app.post("/api/reserve")
    async def reserve_credits(request: Request):
        body = await request.json()
        pay: NexusPay = request.app.state.nexuspay
        reservation = await pay.reserve(
            amount=body["amount"],
            timeout=body.get("timeout", 300),
            purpose=body.get("purpose", "task"),
        )
        return {
            "reservation_id": reservation.id,
            "amount": str(reservation.amount),
            "status": reservation.status,
        }

    @app.post("/api/commit")
    async def commit_reservation(request: Request):
        body = await request.json()
        pay: NexusPay = request.app.state.nexuspay
        await pay.commit(
            reservation_id=body["reservation_id"],
            actual_amount=body.get("actual_amount"),
        )
        return {"status": "committed"}

    return app


@pytest.fixture
def client(app):
    return TestClient(app)


# =============================================================================
# 1. Public Endpoints
# =============================================================================


class TestPublicEndpoints:
    """Test endpoints that don't require payment."""

    def test_health_check(self, client):
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_balance_endpoint(self, client):
        response = client.get("/api/balance")
        assert response.status_code == 200
        data = response.json()
        assert data["available"] == "100.0"
        assert data["reserved"] == "5.0"
        assert data["total"] == "105.0"


# =============================================================================
# 2. Transfer Through Server
# =============================================================================


class TestTransferThroughServer:
    """Test NexusPay transfers exposed via FastAPI."""

    def test_internal_transfer(self, client, credits_service):
        response = client.post(
            "/api/transfer",
            json={"to": "agent-bob", "amount": 5.0, "memo": "Task pay"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["method"] == "credits"
        assert data["amount"] == "5.0"
        assert data["to"] == "agent-bob"
        credits_service.transfer.assert_called_once()

    def test_external_transfer_routes_to_x402(self, client, x402_client):
        """Wallet address destination should route to x402."""
        from nexus.pay.x402 import X402Receipt

        x402_client.pay = AsyncMock(
            return_value=X402Receipt(
                tx_hash="0xdeadbeef" + "00" * 28,
                network="eip155:8453",
                amount=Decimal("2.0"),
                currency="USDC",
                timestamp=None,
            )
        )

        response = client.post(
            "/api/transfer",
            json={
                "to": "0x1234567890abcdef1234567890abcdef12345678",
                "amount": 2.0,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["method"] == "x402"

    def test_negative_amount_rejected(self, client):
        response = client.post(
            "/api/transfer",
            json={"to": "agent-bob", "amount": -1.0},
        )
        assert response.status_code == 400
        assert "positive" in response.json()["detail"].lower()

    def test_zero_amount_rejected(self, client):
        response = client.post(
            "/api/transfer",
            json={"to": "agent-bob", "amount": 0},
        )
        assert response.status_code == 400


# =============================================================================
# 3. Metered Endpoint (per-call charging)
# =============================================================================


class TestMeteredEndpoint:
    """Test per-call metering through server."""

    def test_metered_search_charges(self, client, credits_service):
        credits_service.deduct_fast.return_value = True
        response = client.get("/api/search?q=test")
        assert response.status_code == 200
        data = response.json()
        assert data["charged"] == "0.001"
        credits_service.deduct_fast.assert_called_once()

    def test_metered_search_blocks_on_insufficient(self, client, credits_service):
        credits_service.deduct_fast.return_value = False
        response = client.get("/api/search?q=test")
        assert response.status_code == 402
        assert "insufficient" in response.json()["detail"].lower()


# =============================================================================
# 4. x402 Payment-Gated Endpoints (Middleware)
# =============================================================================


class TestPaymentGatedEndpoints:
    """Test x402 middleware with NexusPay backend."""

    def test_premium_returns_402_without_payment(self, client):
        response = client.get("/api/premium")
        assert response.status_code == 402
        assert "X-Payment-Required" in response.headers

        # Verify payment details
        header = response.headers["X-Payment-Required"]
        payload = json.loads(base64.b64decode(header).decode())
        assert payload["amount"] == "1.00"
        assert payload["currency"] == "USDC"

    def test_premium_works_with_valid_payment(self, client, x402_client):
        """Verified x402 payment should unlock premium endpoint."""

        async def mock_verify(payment_header, expected_amount):
            return X402PaymentVerification(
                valid=True,
                tx_hash="0x" + "ab" * 32,
                amount=expected_amount,
                error=None,
            )

        x402_client.verify_payment = mock_verify

        payment = base64.b64encode(json.dumps({"tx_hash": "0x" + "ab" * 32}).encode()).decode()

        response = client.get(
            "/api/premium",
            headers={"X-Payment": payment},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["paid"] is True

    def test_premium_rejects_invalid_payment(self, client, x402_client):
        async def mock_verify(payment_header, expected_amount):
            return X402PaymentVerification(
                valid=False,
                tx_hash=None,
                amount=None,
                error="Signature mismatch",
            )

        x402_client.verify_payment = mock_verify

        payment = base64.b64encode(b'{"bad": "payment"}').decode()
        response = client.get(
            "/api/premium",
            headers={"X-Payment": payment},
        )
        assert response.status_code == 402
        assert "verification failed" in response.json()["error"].lower()


# =============================================================================
# 5. Two-Phase Reservation Through Server
# =============================================================================


class TestReservationThroughServer:
    """Test reserve/commit flow through API."""

    def test_reserve_and_commit(self, client, credits_service):
        # Step 1: Reserve
        res = client.post(
            "/api/reserve",
            json={"amount": 10.0, "purpose": "task"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "pending"
        assert data["amount"] == "10.0"
        reservation_id = data["reservation_id"]

        # Step 2: Commit with actual amount
        res2 = client.post(
            "/api/commit",
            json={
                "reservation_id": reservation_id,
                "actual_amount": 7.5,
            },
        )
        assert res2.status_code == 200
        assert res2.json()["status"] == "committed"

        credits_service.commit_reservation.assert_called_once_with(
            reservation_id, actual_amount=Decimal("7.5")
        )


# =============================================================================
# 6. Full Payment Lifecycle
# =============================================================================


class TestFullPaymentLifecycle:
    """Test complete payment flows end-to-end."""

    def test_full_lifecycle_check_balance_transfer_meter(self, client, credits_service):
        """Simulate: check balance → transfer → metered search."""
        # 1. Check balance
        res = client.get("/api/balance")
        assert res.status_code == 200
        assert Decimal(res.json()["available"]) > 0

        # 2. Transfer to another agent
        res = client.post(
            "/api/transfer",
            json={"to": "worker-agent", "amount": 5.0, "memo": "Task bounty"},
        )
        assert res.status_code == 200
        assert res.json()["method"] == "credits"

        # 3. Use metered search
        res = client.get("/api/search?q=important+query")
        assert res.status_code == 200
        assert res.json()["charged"] == "0.001"

    def test_reserve_do_work_commit_partial(self, client, credits_service):
        """Simulate: reserve → do work → commit partial amount."""
        # Reserve for a task
        res = client.post(
            "/api/reserve",
            json={"amount": 20.0, "timeout": 600, "purpose": "data_processing"},
        )
        assert res.status_code == 200
        rid = res.json()["reservation_id"]

        # ... simulate work happening ...

        # Commit only what was actually used
        res = client.post(
            "/api/commit",
            json={"reservation_id": rid, "actual_amount": 12.5},
        )
        assert res.status_code == 200

        # Verify partial commit
        credits_service.commit_reservation.assert_called_once_with(
            rid, actual_amount=Decimal("12.5")
        )


# =============================================================================
# 7. NexusPay SDK Direct E2E (no server)
# =============================================================================


class TestNexusPayDirectE2E:
    """Test NexusPay SDK directly with disabled-mode CreditsService."""

    @pytest.mark.asyncio
    async def test_full_sdk_workflow_disabled_mode(self):
        """NexusPay with disabled CreditsService should work end-to-end."""
        service = CreditsService(enabled=False)
        pay = NexusPay(
            api_key="nx_live_e2e_direct",
            credits_service=service,
            x402_enabled=False,
        )

        # Balance
        balance = await pay.get_balance()
        assert isinstance(balance, Balance)
        assert balance.available >= Decimal("999999")

        # Can afford
        assert await pay.can_afford(1000) is True

        # Transfer
        receipt = await pay.transfer(to="bob", amount=50.0, memo="Direct test")
        assert isinstance(receipt, Receipt)
        assert receipt.method == "credits"
        assert receipt.amount == Decimal("50.0")

        # Reserve → commit
        reservation = await pay.reserve(amount=25.0, purpose="e2e_test")
        assert reservation.status == "pending"
        await pay.commit(reservation.id, actual_amount=20.0)

        # Metering
        success = await pay.meter(amount=Decimal("0.001"))
        assert success is True

    @pytest.mark.asyncio
    async def test_budget_context_enforces_limits(self):
        """Budget context should enforce per-tx and daily limits."""
        service = CreditsService(enabled=False)
        pay = NexusPay(
            api_key="nx_live_budget_test",
            credits_service=service,
            x402_enabled=False,
        )

        async with pay.budget(daily=5.0, per_tx=2.0) as agent:
            # Within limits
            r1 = await agent.transfer(to="a", amount=2.0)
            assert r1.amount == Decimal("2.0")

            await agent.transfer(to="b", amount=2.0)
            assert agent.spent == Decimal("4.0")
            assert agent.remaining == Decimal("1.0")

            # Per-tx limit
            with pytest.raises(BudgetExceededError, match="per-transaction"):
                await agent.transfer(to="c", amount=3.0)

            # Daily limit
            with pytest.raises(BudgetExceededError, match="daily"):
                await agent.transfer(to="d", amount=1.5)

    @pytest.mark.asyncio
    async def test_concurrent_transfers(self):
        """Concurrent NexusPay transfers should not interfere."""
        import asyncio

        service = CreditsService(enabled=False)
        pay = NexusPay(
            api_key="nx_live_concurrent",
            credits_service=service,
            x402_enabled=False,
        )

        async def do_transfer(i: int) -> Receipt:
            return await pay.transfer(to=f"agent-{i}", amount=Decimal("1.0"))

        results = await asyncio.gather(*[do_transfer(i) for i in range(50)])
        assert len(results) == 50
        assert all(isinstance(r, Receipt) for r in results)
        # All unique IDs
        assert len({r.id for r in results}) == 50


# =============================================================================
# 8. Pay REST API Multi-Step Flows (Issue #1209)
# =============================================================================


class TestPayRestApiFlows:
    """E2E tests for the pay router REST API endpoints.

    Tests multi-step flows through the actual /api/v2/pay/* endpoints.
    """

    @pytest.fixture
    def pay_app(self, credits_service, x402_client):
        """FastAPI app with the pay router mounted."""
        from nexus.server.api.v2.routers.pay import (
            _register_pay_exception_handlers,
            get_nexuspay,
        )
        from nexus.server.api.v2.routers.pay import (
            router as pay_router,
        )

        app = FastAPI(title="NexusPay REST API E2E")
        app.include_router(pay_router)
        _register_pay_exception_handlers(app)

        app.state.credits_service = credits_service
        app.state.x402_client = x402_client

        # Override auth dependency with a NexusPay instance
        async def _mock_nexuspay():
            return NexusPay(
                api_key="nx_live_e2e_agent",
                credits_service=credits_service,
                x402_client=x402_client,
            )

        app.dependency_overrides[get_nexuspay] = _mock_nexuspay
        return app

    @pytest.fixture
    def pay_client(self, pay_app):
        return TestClient(pay_app)

    def test_reserve_commit_flow(self, pay_client, credits_service):
        """Reserve → Commit: Full two-phase transfer via REST API."""
        # Step 1: Reserve credits
        res = pay_client.post(
            "/api/v2/pay/reserve",
            json={"amount": "20.00", "timeout": 600, "purpose": "task-execution"},
        )
        assert res.status_code == 201
        data = res.json()
        assert data["status"] == "pending"
        assert data["amount"] == "20.00"
        reservation_id = data["id"]

        # Step 2: Commit the reservation
        res2 = pay_client.post(
            f"/api/v2/pay/reserve/{reservation_id}/commit",
            json={"actual_amount": "15.00"},
        )
        assert res2.status_code == 204

        # Verify the commit was called with correct args
        credits_service.commit_reservation.assert_called_once()

    def test_reserve_release_flow(self, pay_client, credits_service):
        """Reserve → Release: Cancel reservation via REST API."""
        # Step 1: Reserve credits
        res = pay_client.post(
            "/api/v2/pay/reserve",
            json={"amount": "30.00", "purpose": "speculative-task"},
        )
        assert res.status_code == 201
        reservation_id = res.json()["id"]

        # Step 2: Release (cancel) the reservation
        res2 = pay_client.post(f"/api/v2/pay/reserve/{reservation_id}/release")
        assert res2.status_code == 204

        credits_service.release_reservation.assert_called_once_with(reservation_id)

    def test_transfer_and_check_balance(self, pay_client, credits_service):
        """Transfer → Balance: Check balance after transfer."""
        # Step 1: Check initial balance
        res = pay_client.get("/api/v2/pay/balance")
        assert res.status_code == 200
        initial = res.json()
        assert initial["available"] == "100.0"

        # Step 2: Transfer
        res2 = pay_client.post(
            "/api/v2/pay/transfer",
            json={"to": "worker-agent", "amount": "25.00", "memo": "Task bounty"},
        )
        assert res2.status_code == 201
        assert res2.json()["method"] == "credits"

        # Step 3: Check balance again (mock still returns same, but verifies flow)
        res3 = pay_client.get("/api/v2/pay/balance")
        assert res3.status_code == 200

    def test_meter_deduction_flow(self, pay_client, credits_service):
        """Meter → Balance: Deduct metered usage and check affordability."""
        # Step 1: Check affordability
        res = pay_client.get("/api/v2/pay/can-afford?amount=0.01")
        assert res.status_code == 200
        assert res.json()["can_afford"] is True

        # Step 2: Meter usage
        res2 = pay_client.post(
            "/api/v2/pay/meter",
            json={"amount": "0.01", "event_type": "api_call"},
        )
        assert res2.status_code == 200
        assert res2.json()["success"] is True

        # Verify metering called deduct_fast
        credits_service.deduct_fast.assert_called_once()

    def test_batch_transfer_flow(self, pay_client, credits_service):
        """Batch Transfer: Atomic multi-agent payment via REST API."""
        res = pay_client.post(
            "/api/v2/pay/transfer/batch",
            json={
                "transfers": [
                    {"to": "agent-a", "amount": "5.00", "memo": "Worker A"},
                    {"to": "agent-b", "amount": "10.00", "memo": "Worker B"},
                ]
            },
        )
        assert res.status_code == 201
        data = res.json()
        assert len(data) == 2
        assert data[0]["id"] == "tx-b1"
        assert data[1]["id"] == "tx-b2"


# =============================================================================
# 9. Module Export Verification
# =============================================================================


class TestModuleExports:
    """Verify NexusPay is properly exported from nexus.pay."""

    def test_sdk_exports_from_pay_package(self):
        from nexus.pay import (
            Balance,
            BudgetContext,
            BudgetExceededError,
            NexusPay,
            NexusPayError,
            Quote,
            Receipt,
            Reservation,
        )

        assert NexusPay is not None
        assert Balance is not None
        assert Receipt is not None
        assert Reservation is not None
        assert Quote is not None
        assert BudgetContext is not None
        assert NexusPayError is not None
        assert BudgetExceededError is not None
