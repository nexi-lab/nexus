"""Integration tests for x402 protocol with FastAPI.

These tests verify the complete x402 flow with a real FastAPI app:
1. Protected endpoints return 402 without payment
2. Webhook endpoint processes payments and credits agents
3. Full topup flow from request to credit
"""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.pay.x402 import X402Client, X402PaymentVerification
from nexus.server.api.v2.routers.x402 import router as x402_router
from nexus.server.middleware.x402 import X402PaymentMiddleware

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def x402_client():
    """Real X402Client for testing."""
    return X402Client(
        facilitator_url="https://x402.org/facilitator",
        wallet_address="0x1234567890123456789012345678901234567890",
        network="base",
        webhook_secret="test-secret-123",
    )


@pytest.fixture
def mock_credits_service():
    """Mock CreditsService that tracks calls."""
    service = AsyncMock()
    service.topup = AsyncMock(return_value="transfer-id-12345")
    service.provision_wallet = AsyncMock()
    service.get_balance = AsyncMock(return_value=Decimal("100.00"))
    return service


@pytest.fixture
def app(x402_client, mock_credits_service):
    """Create full FastAPI app with x402 integration."""
    app = FastAPI(title="Nexus x402 Integration Test")

    # Add x402 router
    app.include_router(x402_router, prefix="/api/v2")

    # Add middleware for protected endpoints
    app.add_middleware(
        X402PaymentMiddleware,
        x402_client=x402_client,
        protected_paths={
            "/api/v2/premium": Decimal("1.00"),
            "/api/v2/expensive": Decimal("10.00"),
        },
    )

    # Set app state
    app.state.x402_client = x402_client
    app.state.credits_service = mock_credits_service

    # Add test endpoints
    @app.get("/api/v2/free")
    async def free_endpoint():
        return {"status": "ok", "data": "free content", "paid": False}

    @app.get("/api/v2/premium")
    async def premium_endpoint():
        return {"status": "ok", "data": "premium content", "paid": True}

    @app.get("/api/v2/expensive")
    async def expensive_endpoint():
        return {"status": "ok", "data": "expensive content", "paid": True}

    return app


@pytest.fixture
def client(app):
    """Test client for the app."""
    return TestClient(app)


# =============================================================================
# Free Endpoint Tests
# =============================================================================


class TestFreeEndpoints:
    """Test endpoints that don't require payment."""

    def test_free_endpoint_works(self, client):
        """Free endpoint should return 200 without any payment."""
        response = client.get("/api/v2/free")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["paid"] is False

    def test_config_endpoint_works(self, client):
        """Config endpoint should return x402 configuration."""
        response = client.get("/api/v2/x402/config")

        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is True
        assert data["network"] == "base"
        assert data["wallet_address"] == "0x1234567890123456789012345678901234567890"


# =============================================================================
# Protected Endpoint Tests
# =============================================================================


class TestProtectedEndpoints:
    """Test endpoints that require x402 payment."""

    def test_premium_endpoint_returns_402_without_payment(self, client):
        """Premium endpoint should return 402 without X-Payment header."""
        response = client.get("/api/v2/premium")

        assert response.status_code == 402
        assert "X-Payment-Required" in response.headers

        # Verify payment details in header
        header = response.headers["X-Payment-Required"]
        payload = json.loads(base64.b64decode(header).decode())

        assert payload["amount"] == "1.00"
        assert payload["currency"] == "USDC"
        assert payload["network"] == "eip155:8453"
        assert payload["address"] == "0x1234567890123456789012345678901234567890"

    def test_expensive_endpoint_returns_402_with_higher_price(self, client):
        """Expensive endpoint should return 402 with higher price."""
        response = client.get("/api/v2/expensive")

        assert response.status_code == 402

        header = response.headers["X-Payment-Required"]
        payload = json.loads(base64.b64decode(header).decode())

        assert payload["amount"] == "10.00"

    def test_premium_endpoint_with_valid_payment(self, client, x402_client):
        """Premium endpoint should work with verified payment."""

        # Mock the verify_payment to return valid
        async def mock_verify(payment_header, expected_amount):
            return X402PaymentVerification(
                valid=True,
                tx_hash="0x" + "ab" * 32,
                amount=expected_amount,
                error=None,
            )

        x402_client.verify_payment = mock_verify

        # Create payment header
        payment = base64.b64encode(
            json.dumps(
                {
                    "tx_hash": "0x" + "ab" * 32,
                    "amount": "1000000",
                    "network": "eip155:8453",
                }
            ).encode()
        ).decode()

        response = client.get(
            "/api/v2/premium",
            headers={"X-Payment": payment},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["paid"] is True

    def test_premium_endpoint_with_invalid_payment(self, client, x402_client):
        """Premium endpoint should return 402 with invalid payment."""

        # Mock the verify_payment to return invalid
        async def mock_verify(payment_header, expected_amount):
            return X402PaymentVerification(
                valid=False,
                tx_hash=None,
                amount=None,
                error="Invalid signature",
            )

        x402_client.verify_payment = mock_verify

        payment = base64.b64encode(b'{"invalid": "payment"}').decode()

        response = client.get(
            "/api/v2/premium",
            headers={"X-Payment": payment},
        )

        assert response.status_code == 402
        data = response.json()
        assert "verification failed" in data["error"].lower()


# =============================================================================
# Webhook Integration Tests
# =============================================================================


class TestWebhookIntegration:
    """Test webhook endpoint with full processing."""

    def test_webhook_credits_agent(self, client, mock_credits_service, x402_client):
        """Webhook should credit agent via CreditsService."""
        # Patch webhook signature verification to pass
        x402_client._verify_webhook_signature = MagicMock(return_value=True)

        payload = {
            "event": "payment.confirmed",
            "tx_hash": "0x" + "cd" * 32,
            "network": "eip155:8453",
            "amount": "5000000",  # 5 USDC
            "currency": "USDC",
            "from": "0xabcdef1234567890abcdef1234567890abcdef12",
            "to": "0x1234567890123456789012345678901234567890",
            "timestamp": "2025-01-15T12:00:00Z",
            "metadata": {
                "agent_id": "agent-test-123",
                "tenant_id": "test-tenant",
            },
            "signature": "valid-test-signature",
        }

        response = client.post("/api/v2/x402/webhook", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "credited"
        assert data["tx_id"] == "transfer-id-12345"

        # Verify CreditsService was called correctly
        mock_credits_service.provision_wallet.assert_called_once_with(
            agent_id="agent-test-123",
            tenant_id="test-tenant",
        )
        mock_credits_service.topup.assert_called_once()
        call_kwargs = mock_credits_service.topup.call_args.kwargs
        assert call_kwargs["agent_id"] == "agent-test-123"
        assert call_kwargs["source"] == "x402"
        assert call_kwargs["external_tx_id"] == "0x" + "cd" * 32

    def test_webhook_rejects_invalid_signature(self, client, x402_client):
        """Webhook should reject invalid signature."""
        # Patch webhook signature verification to fail
        x402_client._verify_webhook_signature = MagicMock(return_value=False)

        payload = {
            "event": "payment.confirmed",
            "tx_hash": "0x" + "cd" * 32,
            "network": "eip155:8453",
            "amount": "5000000",
            "currency": "USDC",
            "from": "0xabcdef1234567890abcdef1234567890abcdef12",
            "to": "0x1234567890123456789012345678901234567890",
            "timestamp": "2025-01-15T12:00:00Z",
            "metadata": {"agent_id": "agent-123"},
            "signature": "invalid-signature",
        }

        response = client.post("/api/v2/x402/webhook", json=payload)

        assert response.status_code == 400
        assert "signature" in response.json()["detail"].lower()

    def test_webhook_rejects_missing_agent_id(self, client, x402_client):
        """Webhook should reject payload without agent_id."""
        x402_client._verify_webhook_signature = MagicMock(return_value=True)

        payload = {
            "event": "payment.confirmed",
            "tx_hash": "0x" + "cd" * 32,
            "network": "eip155:8453",
            "amount": "5000000",
            "currency": "USDC",
            "from": "0xabcdef1234567890abcdef1234567890abcdef12",
            "to": "0x1234567890123456789012345678901234567890",
            "timestamp": "2025-01-15T12:00:00Z",
            "metadata": {},  # No agent_id
            "signature": "valid-signature",
        }

        response = client.post("/api/v2/x402/webhook", json=payload)

        assert response.status_code == 400
        assert "agent_id" in response.json()["detail"].lower()


# =============================================================================
# Topup Flow Tests
# =============================================================================


class TestTopupFlow:
    """Test the complete topup flow."""

    def test_topup_returns_payment_details(self, client):
        """Topup endpoint should return 402 with payment instructions."""
        response = client.post(
            "/api/v2/x402/topup",
            json={
                "agent_id": "agent-buyer-123",
                "amount": "25.00",
                "tenant_id": "default",
            },
        )

        assert response.status_code == 402
        data = response.json()
        assert data["payment_required"] is True
        assert data["amount"] == "25.00"
        assert data["currency"] == "USDC"
        assert data["address"] == "0x1234567890123456789012345678901234567890"

    def test_full_topup_then_webhook_flow(self, client, mock_credits_service, x402_client):
        """Test complete flow: topup request → payment → webhook → credits."""
        # Step 1: Request topup
        topup_response = client.post(
            "/api/v2/x402/topup",
            json={
                "agent_id": "agent-full-flow",
                "amount": "50.00",
            },
        )
        assert topup_response.status_code == 402
        payment_details = topup_response.json()
        assert payment_details["amount"] == "50.00"

        # Step 2: Simulate blockchain payment and webhook
        x402_client._verify_webhook_signature = MagicMock(return_value=True)

        webhook_payload = {
            "event": "payment.confirmed",
            "tx_hash": "0x" + "ef" * 32,
            "network": "eip155:8453",
            "amount": "50000000",  # 50 USDC in micro
            "currency": "USDC",
            "from": "0xbuyer1234567890buyer1234567890buyer12",
            "to": payment_details["address"],
            "timestamp": "2025-01-15T14:30:00Z",
            "metadata": {
                "agent_id": "agent-full-flow",
                "tenant_id": "default",
            },
            "signature": "blockchain-verified-signature",
        }

        webhook_response = client.post("/api/v2/x402/webhook", json=webhook_payload)
        assert webhook_response.status_code == 200
        assert webhook_response.json()["status"] == "credited"

        # Step 3: Verify credits were added
        mock_credits_service.topup.assert_called_once()
        call_kwargs = mock_credits_service.topup.call_args.kwargs
        assert call_kwargs["agent_id"] == "agent-full-flow"
        assert call_kwargs["amount"] == Decimal("50")  # 50000000 micro = 50 USDC


# =============================================================================
# Error Handling Tests
# =============================================================================


class TestErrorHandling:
    """Test error handling in various scenarios."""

    def test_malformed_payment_header(self, client, x402_client):
        """Malformed X-Payment header should be rejected."""

        async def mock_verify(payment_header, expected_amount):
            return X402PaymentVerification(
                valid=False,
                tx_hash=None,
                amount=None,
                error="Invalid payment header format",
            )

        x402_client.verify_payment = mock_verify

        response = client.get(
            "/api/v2/premium",
            headers={"X-Payment": "not-valid-base64!!!"},
        )

        assert response.status_code == 402

    def test_webhook_with_invalid_json(self, client):
        """Webhook should reject invalid JSON."""
        response = client.post(
            "/api/v2/x402/webhook",
            content=b"not valid json",
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 422  # Validation error

    def test_topup_with_missing_fields(self, client):
        """Topup should reject request with missing fields."""
        response = client.post(
            "/api/v2/x402/topup",
            json={
                # Missing agent_id and amount
            },
        )

        assert response.status_code == 422
