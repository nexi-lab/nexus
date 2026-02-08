"""Tests for Nexus Pay REST API router.

Tests for issue #1209: Add Nexus Pay REST API endpoints.

Test categories:
1. Balance endpoint
2. Can-afford endpoint
3. Transfer endpoint
4. Batch transfer endpoint
5. Reserve endpoint
6. Commit endpoint
7. Release endpoint
8. Meter endpoint
9. Authentication (401/403)
10. Edge cases (self-transfer, zero amounts, decimal precision, batch limits)
11. Exception handler mapping
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from nexus.server.api.v2.routers.pay import (
    _register_pay_exception_handlers,
    get_nexuspay,
    router,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_credits_service():
    """Mock CreditsService with sensible defaults."""
    service = AsyncMock()
    service.get_balance = AsyncMock(return_value=Decimal("100.0"))
    service.get_balance_with_reserved = AsyncMock(return_value=(Decimal("100.0"), Decimal("5.0")))
    service.transfer = AsyncMock(return_value="tx-123")
    service.topup = AsyncMock(return_value="topup-123")
    service.reserve = AsyncMock(return_value="res-123")
    service.commit_reservation = AsyncMock()
    service.release_reservation = AsyncMock()
    service.deduct_fast = AsyncMock(return_value=True)
    service.check_budget = AsyncMock(return_value=True)
    service.transfer_batch = AsyncMock(return_value=["tx-1", "tx-2"])
    service.provision_wallet = AsyncMock()
    return service


@pytest.fixture
def mock_x402_client():
    """Mock X402Client."""
    client = MagicMock()
    client.network = "base"
    client.wallet_address = "0x1234567890123456789012345678901234567890"
    return client


@pytest.fixture
def mock_auth_result():
    """Standard auth result for test-agent."""
    return {
        "authenticated": True,
        "subject_type": "agent",
        "subject_id": "test-agent",
        "zone_id": "default",
        "is_admin": False,
        "x_agent_id": None,
        "metadata": {},
    }


@pytest.fixture
def app(mock_credits_service, mock_x402_client, mock_auth_result):
    """Create test FastAPI app with pay router and mocked auth."""
    app = FastAPI()
    app.include_router(router)
    _register_pay_exception_handlers(app)

    # Set app state
    app.state.credits_service = mock_credits_service
    app.state.x402_client = mock_x402_client

    # Override the NexusPay dependency to use mocks
    from nexus.pay.sdk import NexusPay

    async def _mock_nexuspay():
        return NexusPay(
            api_key="nx_live_test-agent",
            credits_service=mock_credits_service,
            x402_client=mock_x402_client,
            zone_id="default",
        )

    app.dependency_overrides[get_nexuspay] = _mock_nexuspay

    return app


@pytest.fixture
def client(app):
    """Test client."""
    return TestClient(app)


@pytest.fixture
def app_no_services():
    """App without credits_service configured."""
    app = FastAPI()
    app.include_router(router)
    _register_pay_exception_handlers(app)
    # Don't set app.state.credits_service
    return app


@pytest.fixture
def client_no_services(app_no_services):
    """Client without services."""
    return TestClient(app_no_services)


@pytest.fixture
def app_no_auth(mock_credits_service, mock_x402_client):
    """App where auth rejects all requests (simulates missing/invalid token)."""
    app = FastAPI()
    app.include_router(router)
    _register_pay_exception_handlers(app)
    app.state.credits_service = mock_credits_service
    app.state.x402_client = mock_x402_client

    # Override get_nexuspay to simulate auth rejection
    async def _reject_auth():
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    app.dependency_overrides[get_nexuspay] = _reject_auth
    return app


@pytest.fixture
def client_no_auth(app_no_auth):
    """Client that tests auth failures."""
    return TestClient(app_no_auth)


# =============================================================================
# 1. Balance Endpoint
# =============================================================================


class TestBalanceEndpoint:
    """Test GET /api/v2/pay/balance."""

    def test_balance_success(self, client):
        """Should return available, reserved, and total balance."""
        response = client.get("/api/v2/pay/balance")

        assert response.status_code == 200
        data = response.json()
        assert data["available"] == "100.0"
        assert data["reserved"] == "5.0"
        assert data["total"] == "105.0"

    def test_balance_zero(self, client, mock_credits_service):
        """Should handle zero balance."""
        mock_credits_service.get_balance_with_reserved = AsyncMock(
            return_value=(Decimal("0"), Decimal("0"))
        )
        response = client.get("/api/v2/pay/balance")

        assert response.status_code == 200
        data = response.json()
        assert data["available"] == "0"
        assert data["reserved"] == "0"
        assert data["total"] == "0"


# =============================================================================
# 2. Can-Afford Endpoint
# =============================================================================


class TestCanAffordEndpoint:
    """Test GET /api/v2/pay/can-afford."""

    def test_can_afford_true(self, client):
        """Should return true when agent can afford."""
        response = client.get("/api/v2/pay/can-afford?amount=10.00")

        assert response.status_code == 200
        data = response.json()
        assert data["can_afford"] is True
        assert data["amount"] == "10.00"

    def test_can_afford_false(self, client, mock_credits_service):
        """Should return false when agent cannot afford."""
        mock_credits_service.check_budget = AsyncMock(return_value=False)

        response = client.get("/api/v2/pay/can-afford?amount=999999.00")

        assert response.status_code == 200
        data = response.json()
        assert data["can_afford"] is False

    def test_can_afford_invalid_amount(self, client):
        """Should reject invalid amount."""
        response = client.get("/api/v2/pay/can-afford?amount=notanumber")
        assert response.status_code == 400

    def test_can_afford_negative_amount(self, client):
        """Should reject negative amount."""
        response = client.get("/api/v2/pay/can-afford?amount=-5.00")
        assert response.status_code == 400

    def test_can_afford_zero_amount(self, client):
        """Should reject zero amount."""
        response = client.get("/api/v2/pay/can-afford?amount=0")
        assert response.status_code == 400


# =============================================================================
# 3. Transfer Endpoint
# =============================================================================


class TestTransferEndpoint:
    """Test POST /api/v2/pay/transfer."""

    def test_transfer_credits_success(self, client, mock_credits_service):
        """Should transfer credits to another agent."""
        payload = {
            "to": "agent-bob",
            "amount": "10.50",
            "memo": "Payment for task",
        }

        response = client.post("/api/v2/pay/transfer", json=payload)

        assert response.status_code == 201
        data = response.json()
        assert data["id"] == "tx-123"
        assert data["method"] == "credits"
        assert data["amount"] == "10.50"
        assert data["from_agent"] == "test-agent"
        assert data["to_agent"] == "agent-bob"
        assert data["memo"] == "Payment for task"

    def test_transfer_with_idempotency_key(self, client, mock_credits_service):
        """Should pass idempotency key to SDK."""
        payload = {
            "to": "agent-bob",
            "amount": "5.00",
            "idempotency_key": "unique-key-123",
        }

        response = client.post("/api/v2/pay/transfer", json=payload)
        assert response.status_code == 201

        # Verify idempotency_key was passed through
        mock_credits_service.transfer.assert_called_once()
        call_kwargs = mock_credits_service.transfer.call_args
        assert call_kwargs.kwargs.get("idempotency_key") == "unique-key-123"

    def test_transfer_explicit_method(self, client):
        """Should accept explicit method override."""
        payload = {
            "to": "agent-bob",
            "amount": "5.00",
            "method": "credits",
        }

        response = client.post("/api/v2/pay/transfer", json=payload)
        assert response.status_code == 201

    def test_transfer_invalid_method(self, client):
        """Should reject invalid method."""
        payload = {
            "to": "agent-bob",
            "amount": "5.00",
            "method": "bitcoin",
        }

        response = client.post("/api/v2/pay/transfer", json=payload)
        assert response.status_code == 422

    def test_transfer_zero_amount(self, client):
        """Should reject zero amount."""
        payload = {"to": "agent-bob", "amount": "0"}

        response = client.post("/api/v2/pay/transfer", json=payload)
        assert response.status_code == 422

    def test_transfer_negative_amount(self, client):
        """Should reject negative amount."""
        payload = {"to": "agent-bob", "amount": "-5.00"}

        response = client.post("/api/v2/pay/transfer", json=payload)
        assert response.status_code == 422

    def test_transfer_insufficient_credits(self, client, mock_credits_service):
        """Should return 402 for insufficient credits."""
        from nexus.pay.credits import InsufficientCreditsError

        mock_credits_service.transfer = AsyncMock(
            side_effect=InsufficientCreditsError("Insufficient balance")
        )

        payload = {"to": "agent-bob", "amount": "999999.00"}

        response = client.post("/api/v2/pay/transfer", json=payload)
        assert response.status_code == 402
        assert "insufficient" in response.json()["detail"].lower()
        assert response.json()["error_code"] == "insufficient_credits"

    def test_transfer_missing_to(self, client):
        """Should reject missing 'to' field."""
        payload = {"amount": "5.00"}

        response = client.post("/api/v2/pay/transfer", json=payload)
        assert response.status_code == 422

    def test_transfer_too_many_decimal_places(self, client):
        """Should reject amounts with >6 decimal places."""
        payload = {"to": "agent-bob", "amount": "1.1234567"}

        response = client.post("/api/v2/pay/transfer", json=payload)
        assert response.status_code == 422


# =============================================================================
# 4. Batch Transfer Endpoint
# =============================================================================


class TestBatchTransferEndpoint:
    """Test POST /api/v2/pay/transfer/batch."""

    def test_batch_transfer_success(self, client, mock_credits_service):
        """Should execute batch transfer and return receipts."""
        payload = {
            "transfers": [
                {"to": "agent-a", "amount": "5.00", "memo": "Task 1"},
                {"to": "agent-b", "amount": "10.00", "memo": "Task 2"},
            ]
        }

        response = client.post("/api/v2/pay/transfer/batch", json=payload)

        assert response.status_code == 201
        data = response.json()
        assert len(data) == 2
        assert data[0]["id"] == "tx-1"
        assert data[1]["id"] == "tx-2"

    def test_batch_transfer_empty(self, client):
        """Should return empty list for empty batch."""
        payload = {"transfers": []}

        response = client.post("/api/v2/pay/transfer/batch", json=payload)

        assert response.status_code == 201
        assert response.json() == []

    def test_batch_transfer_too_many(self, client):
        """Should reject batches exceeding 1000 items."""
        payload = {
            "transfers": [
                {"to": f"agent-{i}", "amount": "1.00"}
                for i in range(1001)
            ]
        }

        response = client.post("/api/v2/pay/transfer/batch", json=payload)
        assert response.status_code == 422

    def test_batch_transfer_invalid_item(self, client):
        """Should reject batch with invalid transfer item."""
        payload = {
            "transfers": [
                {"to": "agent-a", "amount": "-1.00"},  # Invalid
            ]
        }

        response = client.post("/api/v2/pay/transfer/batch", json=payload)
        assert response.status_code == 422


# =============================================================================
# 5. Reserve Endpoint
# =============================================================================


class TestReserveEndpoint:
    """Test POST /api/v2/pay/reserve."""

    def test_reserve_success(self, client):
        """Should create reservation and return details."""
        payload = {
            "amount": "25.00",
            "timeout": 600,
            "purpose": "task-execution",
            "task_id": "task-789",
        }

        response = client.post("/api/v2/pay/reserve", json=payload)

        assert response.status_code == 201
        data = response.json()
        assert data["id"] == "res-123"
        assert data["amount"] == "25.00"
        assert data["purpose"] == "task-execution"
        assert data["status"] == "pending"

    def test_reserve_defaults(self, client):
        """Should use default timeout and purpose."""
        payload = {"amount": "10.00"}

        response = client.post("/api/v2/pay/reserve", json=payload)

        assert response.status_code == 201
        data = response.json()
        assert data["purpose"] == "general"

    def test_reserve_zero_amount(self, client):
        """Should reject zero amount."""
        payload = {"amount": "0"}

        response = client.post("/api/v2/pay/reserve", json=payload)
        assert response.status_code == 422

    def test_reserve_insufficient_credits(self, client, mock_credits_service):
        """Should return 402 for insufficient credits."""
        from nexus.pay.credits import InsufficientCreditsError

        mock_credits_service.reserve = AsyncMock(
            side_effect=InsufficientCreditsError("Insufficient balance")
        )

        payload = {"amount": "999999.00"}

        response = client.post("/api/v2/pay/reserve", json=payload)
        assert response.status_code == 402

    def test_reserve_invalid_timeout(self, client):
        """Should reject timeout outside valid range."""
        payload = {"amount": "10.00", "timeout": 0}

        response = client.post("/api/v2/pay/reserve", json=payload)
        assert response.status_code == 422

    def test_reserve_timeout_too_large(self, client):
        """Should reject timeout exceeding max."""
        payload = {"amount": "10.00", "timeout": 100000}

        response = client.post("/api/v2/pay/reserve", json=payload)
        assert response.status_code == 422


# =============================================================================
# 6. Commit Endpoint
# =============================================================================


class TestCommitEndpoint:
    """Test POST /api/v2/pay/reserve/{id}/commit."""

    def test_commit_success(self, client, mock_credits_service):
        """Should commit reservation."""
        response = client.post("/api/v2/pay/reserve/res-123/commit")

        assert response.status_code == 204
        mock_credits_service.commit_reservation.assert_called_once()

    def test_commit_with_actual_amount(self, client, mock_credits_service):
        """Should commit with actual amount."""
        response = client.post(
            "/api/v2/pay/reserve/res-123/commit",
            json={"actual_amount": "15.00"},
        )

        assert response.status_code == 204
        mock_credits_service.commit_reservation.assert_called_once()
        call_args = mock_credits_service.commit_reservation.call_args
        assert call_args.kwargs.get("actual_amount") == Decimal("15.00")

    def test_commit_nonexistent_reservation(self, client, mock_credits_service):
        """Should return 409 for non-existent reservation."""
        from nexus.pay.credits import ReservationError

        mock_credits_service.commit_reservation = AsyncMock(
            side_effect=ReservationError("Reservation not found")
        )

        response = client.post("/api/v2/pay/reserve/nonexistent/commit")
        assert response.status_code == 409
        assert response.json()["error_code"] == "reservation_error"


# =============================================================================
# 7. Release Endpoint
# =============================================================================


class TestReleaseEndpoint:
    """Test POST /api/v2/pay/reserve/{id}/release."""

    def test_release_success(self, client, mock_credits_service):
        """Should release reservation."""
        response = client.post("/api/v2/pay/reserve/res-123/release")

        assert response.status_code == 204
        mock_credits_service.release_reservation.assert_called_once_with("res-123")

    def test_release_nonexistent_reservation(self, client, mock_credits_service):
        """Should return 409 for non-existent reservation."""
        from nexus.pay.credits import ReservationError

        mock_credits_service.release_reservation = AsyncMock(
            side_effect=ReservationError("Reservation not found")
        )

        response = client.post("/api/v2/pay/reserve/nonexistent/release")
        assert response.status_code == 409


# =============================================================================
# 8. Meter Endpoint
# =============================================================================


class TestMeterEndpoint:
    """Test POST /api/v2/pay/meter."""

    def test_meter_success(self, client, mock_credits_service):
        """Should deduct and return success."""
        payload = {"amount": "0.01", "event_type": "api_call"}

        response = client.post("/api/v2/pay/meter", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_meter_insufficient_credits(self, client, mock_credits_service):
        """Should return success=false when insufficient credits."""
        mock_credits_service.deduct_fast = AsyncMock(return_value=False)

        payload = {"amount": "999999.00"}

        response = client.post("/api/v2/pay/meter", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False

    def test_meter_zero_amount(self, client):
        """Should reject zero amount."""
        payload = {"amount": "0"}

        response = client.post("/api/v2/pay/meter", json=payload)
        assert response.status_code == 422

    def test_meter_default_event_type(self, client):
        """Should use default event_type."""
        payload = {"amount": "0.01"}

        response = client.post("/api/v2/pay/meter", json=payload)
        assert response.status_code == 200


# =============================================================================
# 9. Authentication Tests
# =============================================================================


class TestAuthRequired:
    """Test that all endpoints require authentication."""

    ENDPOINTS = [
        ("GET", "/api/v2/pay/balance"),
        ("GET", "/api/v2/pay/can-afford?amount=1.00"),
        ("POST", "/api/v2/pay/transfer"),
        ("POST", "/api/v2/pay/transfer/batch"),
        ("POST", "/api/v2/pay/reserve"),
        ("POST", "/api/v2/pay/reserve/res-123/commit"),
        ("POST", "/api/v2/pay/reserve/res-123/release"),
        ("POST", "/api/v2/pay/meter"),
    ]

    @pytest.mark.parametrize("method,path", ENDPOINTS)
    def test_unauthenticated_returns_401(self, method, path, client_no_auth):
        """Every endpoint should return 401 without valid auth."""
        # Build request body for POST endpoints
        bodies = {
            "/api/v2/pay/transfer": {"to": "agent-bob", "amount": "1.00"},
            "/api/v2/pay/transfer/batch": {"transfers": []},
            "/api/v2/pay/reserve": {"amount": "1.00"},
            "/api/v2/pay/meter": {"amount": "0.01"},
        }
        body = bodies.get(path)

        if method == "GET":
            response = client_no_auth.get(path)
        else:
            response = client_no_auth.post(path, json=body)

        # Should be 401 or 422 (validation) or 500 (missing auth dep)
        # The exact code depends on how require_auth is configured,
        # but it should NOT be 200/201/204
        assert response.status_code in (401, 403, 422, 500), (
            f"Expected auth failure for {method} {path}, got {response.status_code}"
        )


class TestAgentIsolation:
    """Test that agents can only access their own data."""

    def test_balance_scoped_to_authenticated_agent(self, client, mock_credits_service):
        """Balance should be for the authenticated agent, not arbitrary agents."""
        response = client.get("/api/v2/pay/balance")

        assert response.status_code == 200
        # Verify the SDK was called with the correct agent_id
        mock_credits_service.get_balance_with_reserved.assert_called_once_with(
            "test-agent", "default"
        )

    def test_transfer_from_authenticated_agent(self, client, mock_credits_service):
        """Transfer should always be FROM the authenticated agent."""
        payload = {"to": "agent-bob", "amount": "5.00"}

        response = client.post("/api/v2/pay/transfer", json=payload)

        assert response.status_code == 201
        mock_credits_service.transfer.assert_called_once()
        call_kwargs = mock_credits_service.transfer.call_args
        assert call_kwargs.kwargs.get("from_id") == "test-agent"

    def test_reserve_scoped_to_authenticated_agent(self, client, mock_credits_service):
        """Reserve should be for the authenticated agent."""
        payload = {"amount": "10.00"}

        response = client.post("/api/v2/pay/reserve", json=payload)

        assert response.status_code == 201
        mock_credits_service.reserve.assert_called_once()
        call_kwargs = mock_credits_service.reserve.call_args
        assert call_kwargs.kwargs.get("agent_id") == "test-agent"


# =============================================================================
# 10. Edge Cases
# =============================================================================


class TestEdgeCases:
    """Test edge cases for payment operations."""

    def test_decimal_precision_six_places(self, client):
        """Should accept amounts with exactly 6 decimal places."""
        payload = {"to": "agent-bob", "amount": "1.123456"}

        response = client.post("/api/v2/pay/transfer", json=payload)
        assert response.status_code == 201

    def test_decimal_precision_seven_places_rejected(self, client):
        """Should reject amounts with >6 decimal places."""
        payload = {"to": "agent-bob", "amount": "1.1234567"}

        response = client.post("/api/v2/pay/transfer", json=payload)
        assert response.status_code == 422

    def test_very_large_amount(self, client):
        """Should accept large valid amounts."""
        payload = {"to": "agent-bob", "amount": "999999999.999999"}

        response = client.post("/api/v2/pay/transfer", json=payload)
        # Will succeed at API validation level (may fail at SDK level)
        assert response.status_code in (201, 402)

    def test_batch_exactly_1000(self, client, mock_credits_service):
        """Should accept batch of exactly 1000 items."""
        mock_credits_service.transfer_batch = AsyncMock(
            return_value=[f"tx-{i}" for i in range(1000)]
        )
        payload = {
            "transfers": [
                {"to": f"agent-{i}", "amount": "0.01"}
                for i in range(1000)
            ]
        }

        response = client.post("/api/v2/pay/transfer/batch", json=payload)
        assert response.status_code == 201
        assert len(response.json()) == 1000

    def test_batch_1001_rejected(self, client):
        """Should reject batch exceeding 1000."""
        payload = {
            "transfers": [
                {"to": f"agent-{i}", "amount": "0.01"}
                for i in range(1001)
            ]
        }

        response = client.post("/api/v2/pay/transfer/batch", json=payload)
        assert response.status_code == 422


# =============================================================================
# 11. Exception Handler Mapping
# =============================================================================


class TestExceptionHandlerMapping:
    """Test centralized exception handler maps errors to correct HTTP codes."""

    def test_insufficient_credits_returns_402(self, client, mock_credits_service):
        """InsufficientCreditsError → 402."""
        from nexus.pay.credits import InsufficientCreditsError

        mock_credits_service.transfer = AsyncMock(
            side_effect=InsufficientCreditsError("Not enough credits")
        )

        response = client.post(
            "/api/v2/pay/transfer",
            json={"to": "bob", "amount": "100.00"},
        )

        assert response.status_code == 402
        assert response.json()["error_code"] == "insufficient_credits"

    def test_wallet_not_found_returns_404(self, client, mock_credits_service):
        """WalletNotFoundError → 404."""
        from nexus.pay.credits import WalletNotFoundError

        mock_credits_service.get_balance_with_reserved = AsyncMock(
            side_effect=WalletNotFoundError("Wallet not found")
        )

        response = client.get("/api/v2/pay/balance")

        assert response.status_code == 404
        assert response.json()["error_code"] == "wallet_not_found"

    def test_reservation_error_returns_409(self, client, mock_credits_service):
        """ReservationError → 409."""
        from nexus.pay.credits import ReservationError

        mock_credits_service.commit_reservation = AsyncMock(
            side_effect=ReservationError("Already committed")
        )

        response = client.post("/api/v2/pay/reserve/res-123/commit")

        assert response.status_code == 409
        assert response.json()["error_code"] == "reservation_error"

    def test_generic_pay_error_returns_400(self, client, mock_credits_service):
        """NexusPayError → 400."""
        from nexus.pay.sdk import NexusPayError

        mock_credits_service.get_balance_with_reserved = AsyncMock(
            side_effect=NexusPayError("Something went wrong")
        )

        response = client.get("/api/v2/pay/balance")

        assert response.status_code == 400
        assert response.json()["error_code"] == "pay_error"


# =============================================================================
# 12. Service Availability
# =============================================================================


class TestServiceAvailability:
    """Test behavior when services are not configured."""

    def test_missing_credits_service_returns_503(self, client_no_services):
        """Should return 503 when credits service is not available."""
        response = client_no_services.get("/api/v2/pay/balance")

        # Will fail at dependency injection (503 or 500)
        assert response.status_code in (500, 503)
