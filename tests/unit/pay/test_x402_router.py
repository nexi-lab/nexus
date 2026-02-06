"""Tests for x402 FastAPI router endpoints.

These tests verify:
1. Webhook endpoint for payment confirmations
2. Topup endpoint for credit purchase initiation
3. Configuration endpoint
4. Error handling
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v2.routers.x402 import router

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_x402_client():
    """Mock X402Client for testing."""
    client = MagicMock()
    client.network = "base"
    client.facilitator_url = "https://x402.org/facilitator"
    client.wallet_address = "0x1234567890123456789012345678901234567890"

    # Mock payment_required_response
    import base64
    import json

    mock_response = MagicMock()
    mock_response.headers = {
        "X-Payment-Required": base64.b64encode(
            json.dumps(
                {
                    "amount": "10.00",
                    "currency": "USDC",
                    "network": "eip155:8453",
                    "address": "0x1234567890123456789012345678901234567890",
                    "description": "Credit topup",
                }
            ).encode()
        ).decode()
    }
    client.payment_required_response = MagicMock(return_value=mock_response)

    # Mock process_topup_webhook as async
    client.process_topup_webhook = AsyncMock(return_value="transfer-123")

    return client


@pytest.fixture
def mock_credits_service():
    """Mock CreditsService for testing."""
    service = AsyncMock()
    service.topup = AsyncMock(return_value="transfer-123")
    service.provision_wallet = AsyncMock()
    return service


@pytest.fixture
def app(mock_x402_client, mock_credits_service):
    """Create test FastAPI app with x402 router."""
    app = FastAPI()
    app.include_router(router, prefix="/api/v2")

    # Set app state
    app.state.x402_client = mock_x402_client
    app.state.credits_service = mock_credits_service

    return app


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def app_without_x402():
    """Create test FastAPI app without x402 configured."""
    app = FastAPI()
    app.include_router(router, prefix="/api/v2")
    return app


@pytest.fixture
def client_without_x402(app_without_x402):
    """Create test client without x402."""
    return TestClient(app_without_x402)


# =============================================================================
# Webhook Endpoint Tests
# =============================================================================


class TestWebhookEndpoint:
    """Test x402 webhook endpoint."""

    def test_webhook_success(self, client, mock_x402_client):
        """Webhook should process payment and return success."""
        payload = {
            "event": "payment.confirmed",
            "tx_hash": "0x" + "ab" * 32,
            "network": "eip155:8453",
            "amount": "1000000",
            "currency": "USDC",
            "from": "0xabcdef1234567890abcdef1234567890abcdef12",
            "to": "0x1234567890123456789012345678901234567890",
            "timestamp": "2025-01-01T00:00:00Z",
            "metadata": {"agent_id": "agent-123"},
            "signature": "valid-signature",
        }

        response = client.post("/api/v2/x402/webhook", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "credited"
        assert data["tx_id"] == "transfer-123"

    def test_webhook_invalid_signature(self, client, mock_x402_client):
        """Webhook should reject invalid signature."""
        from nexus.pay.x402 import X402Error

        mock_x402_client.process_topup_webhook = AsyncMock(
            side_effect=X402Error("Invalid webhook signature")
        )

        payload = {
            "event": "payment.confirmed",
            "tx_hash": "0x" + "ab" * 32,
            "network": "eip155:8453",
            "amount": "1000000",
            "currency": "USDC",
            "from": "0xabcdef1234567890abcdef1234567890abcdef12",
            "to": "0x1234567890123456789012345678901234567890",
            "timestamp": "2025-01-01T00:00:00Z",
            "metadata": {"agent_id": "agent-123"},
            "signature": "invalid-signature",
        }

        response = client.post("/api/v2/x402/webhook", json=payload)

        assert response.status_code == 400
        assert "signature" in response.json()["detail"].lower()

    def test_webhook_missing_x402_client(self, client_without_x402):
        """Webhook should return 503 if x402 not configured."""
        payload = {
            "event": "payment.confirmed",
            "tx_hash": "0x" + "ab" * 32,
            "network": "eip155:8453",
            "amount": "1000000",
            "currency": "USDC",
            "from": "0xabcdef1234567890abcdef1234567890abcdef12",
            "to": "0x1234567890123456789012345678901234567890",
            "timestamp": "2025-01-01T00:00:00Z",
            "metadata": {"agent_id": "agent-123"},
            "signature": "valid-signature",
        }

        response = client_without_x402.post("/api/v2/x402/webhook", json=payload)

        assert response.status_code == 503
        assert "not configured" in response.json()["detail"].lower()

    def test_webhook_missing_required_fields(self, client):
        """Webhook should reject payload missing required fields."""
        payload = {
            "event": "payment.confirmed",
            # Missing tx_hash and other required fields
        }

        response = client.post("/api/v2/x402/webhook", json=payload)

        assert response.status_code == 422  # Validation error


# =============================================================================
# Topup Endpoint Tests
# =============================================================================


class TestTopupEndpoint:
    """Test x402 topup endpoint."""

    def test_topup_returns_402(self, client):
        """Topup should return 402 with payment details."""
        payload = {
            "agent_id": "agent-123",
            "amount": "10.00",
            "tenant_id": "default",
        }

        response = client.post("/api/v2/x402/topup", json=payload)

        assert response.status_code == 402
        data = response.json()
        assert data["payment_required"] is True
        assert data["amount"] == "10.00"
        assert data["currency"] == "USDC"
        assert data["address"] == "0x1234567890123456789012345678901234567890"

    def test_topup_missing_x402_client(self, client_without_x402):
        """Topup should return 503 if x402 not configured."""
        payload = {
            "agent_id": "agent-123",
            "amount": "10.00",
        }

        response = client_without_x402.post("/api/v2/x402/topup", json=payload)

        assert response.status_code == 503

    def test_topup_invalid_amount(self, client, mock_x402_client):
        """Topup should handle invalid amount."""
        from nexus.pay.x402 import X402Error

        mock_x402_client.payment_required_response = MagicMock(
            side_effect=X402Error("amount must be positive")
        )

        payload = {
            "agent_id": "agent-123",
            "amount": "-10.00",
        }

        response = client.post("/api/v2/x402/topup", json=payload)

        assert response.status_code == 400


# =============================================================================
# Config Endpoint Tests
# =============================================================================


class TestConfigEndpoint:
    """Test x402 configuration endpoint."""

    def test_config_enabled(self, client):
        """Config should return enabled status when x402 is configured."""
        response = client.get("/api/v2/x402/config")

        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is True
        assert data["network"] == "base"
        assert data["wallet_address"] == "0x1234567890123456789012345678901234567890"

    def test_config_disabled(self, client_without_x402):
        """Config should return disabled status when x402 not configured."""
        response = client_without_x402.get("/api/v2/x402/config")

        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is False
        assert data["wallet_address"] is None


# =============================================================================
# Integration Tests
# =============================================================================


class TestX402Integration:
    """Integration tests for x402 flow."""

    def test_full_topup_flow(self, client, mock_x402_client, mock_credits_service):
        """Test complete topup flow: request -> payment -> webhook."""
        # Step 1: Request topup, get payment details
        topup_payload = {
            "agent_id": "agent-123",
            "amount": "10.00",
        }

        topup_response = client.post("/api/v2/x402/topup", json=topup_payload)
        assert topup_response.status_code == 402

        payment_details = topup_response.json()
        assert payment_details["amount"] == "10.00"

        # Step 2: Simulate payment confirmation via webhook
        webhook_payload = {
            "event": "payment.confirmed",
            "tx_hash": "0x" + "ab" * 32,
            "network": "eip155:8453",
            "amount": "10000000",  # 10 USDC in micro units
            "currency": "USDC",
            "from": "0xabcdef1234567890abcdef1234567890abcdef12",
            "to": payment_details["address"],
            "timestamp": "2025-01-01T00:00:00Z",
            "metadata": {"agent_id": "agent-123", "tenant_id": "default"},
            "signature": "valid-signature",
        }

        webhook_response = client.post("/api/v2/x402/webhook", json=webhook_payload)
        assert webhook_response.status_code == 200
        assert webhook_response.json()["status"] == "credited"
