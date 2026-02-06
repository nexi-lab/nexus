"""Tests for x402 payment middleware.

These tests verify:
1. Middleware allows free endpoints through
2. Middleware returns 402 for protected endpoints without payment
3. Middleware verifies payment and allows if valid
4. Middleware returns 402 if payment invalid
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.pay.x402 import X402PaymentVerification
from nexus.server.middleware.x402 import X402PaymentMiddleware


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_x402_client():
    """Mock X402Client for testing."""
    client = MagicMock()
    client.network = "base"
    client.wallet_address = "0x1234567890123456789012345678901234567890"

    # Mock payment_required_response
    import base64
    import json

    from starlette.responses import Response

    def mock_payment_required(amount, description="API access"):
        payload = {
            "amount": str(amount),
            "currency": "USDC",
            "network": "eip155:8453",
            "address": "0x1234567890123456789012345678901234567890",
            "description": description,
        }
        return Response(
            status_code=402,
            content=json.dumps({"error": "Payment required"}).encode(),
            media_type="application/json",
            headers={"X-Payment-Required": base64.b64encode(json.dumps(payload).encode()).decode()},
        )

    client.payment_required_response = MagicMock(side_effect=mock_payment_required)

    # Mock verify_payment - will be overridden in specific tests
    async def mock_verify(payment_header, expected_amount):
        return X402PaymentVerification(
            valid=True,
            tx_hash="0x" + "ab" * 32,
            amount=expected_amount,
            error=None,
        )

    client.verify_payment = AsyncMock(side_effect=mock_verify)

    return client


@pytest.fixture
def app_with_middleware(mock_x402_client):
    """Create FastAPI app with x402 middleware."""
    app = FastAPI()

    # Add middleware
    app.add_middleware(
        X402PaymentMiddleware,
        x402_client=mock_x402_client,
        protected_paths={
            "/api/premium": Decimal("1.00"),
            "/api/expensive": Decimal("10.00"),
        },
    )

    # Add test endpoints
    @app.get("/api/free")
    async def free_endpoint():
        return {"data": "free content"}

    @app.get("/api/premium")
    async def premium_endpoint():
        return {"data": "premium content"}

    @app.get("/api/expensive")
    async def expensive_endpoint():
        return {"data": "expensive content"}

    return app


@pytest.fixture
def client(app_with_middleware):
    """Create test client."""
    return TestClient(app_with_middleware)


# =============================================================================
# Free Endpoint Tests
# =============================================================================


class TestFreeEndpoints:
    """Test that free endpoints work without payment."""

    def test_free_endpoint_no_payment_needed(self, client):
        """Free endpoints should work without X-Payment header."""
        response = client.get("/api/free")

        assert response.status_code == 200
        assert response.json()["data"] == "free content"


# =============================================================================
# Protected Endpoint Tests
# =============================================================================


class TestProtectedEndpoints:
    """Test protected endpoints requiring payment."""

    def test_protected_endpoint_requires_payment(self, client):
        """Protected endpoint should return 402 without payment."""
        response = client.get("/api/premium")

        assert response.status_code == 402
        assert "X-Payment-Required" in response.headers

    def test_protected_endpoint_with_valid_payment(self, client, mock_x402_client):
        """Protected endpoint should work with valid payment."""
        # Set up mock to return valid verification
        mock_x402_client.verify_payment = AsyncMock(
            return_value=X402PaymentVerification(
                valid=True,
                tx_hash="0x" + "ab" * 32,
                amount=Decimal("1.00"),
                error=None,
            )
        )

        import base64
        import json

        payment = base64.b64encode(json.dumps({"tx_hash": "0x" + "ab" * 32}).encode()).decode()

        response = client.get(
            "/api/premium",
            headers={"X-Payment": payment},
        )

        assert response.status_code == 200
        assert response.json()["data"] == "premium content"

    def test_protected_endpoint_with_invalid_payment(self, client, mock_x402_client):
        """Protected endpoint should return 402 with invalid payment."""
        # Set up mock to return invalid verification
        mock_x402_client.verify_payment = AsyncMock(
            return_value=X402PaymentVerification(
                valid=False,
                tx_hash=None,
                amount=None,
                error="Invalid signature",
            )
        )

        import base64
        import json

        payment = base64.b64encode(json.dumps({"tx_hash": "invalid"}).encode()).decode()

        response = client.get(
            "/api/premium",
            headers={"X-Payment": payment},
        )

        assert response.status_code == 402
        data = response.json()
        assert "verification failed" in data["error"].lower()

    def test_different_prices_for_different_paths(self, client, mock_x402_client):
        """Different paths should have different prices."""
        # Request premium (1.00)
        response1 = client.get("/api/premium")
        assert response1.status_code == 402

        # Request expensive (10.00)
        response2 = client.get("/api/expensive")
        assert response2.status_code == 402

        # Verify different amounts requested
        calls = mock_x402_client.payment_required_response.call_args_list
        assert len(calls) == 2
        assert calls[0].kwargs["amount"] == Decimal("1.00")
        assert calls[1].kwargs["amount"] == Decimal("10.00")


# =============================================================================
# Dynamic Pricing Tests
# =============================================================================


class TestDynamicPricing:
    """Test dynamic pricing callback."""

    def test_dynamic_pricing_callback(self, mock_x402_client):
        """Dynamic pricing callback should override static paths."""
        app = FastAPI()

        def price_callback(request):
            # Premium users get discount
            if request.headers.get("X-User-Type") == "premium":
                return Decimal("0.50")
            return Decimal("1.00")

        app.add_middleware(
            X402PaymentMiddleware,
            x402_client=mock_x402_client,
            price_callback=price_callback,
        )

        @app.get("/api/data")
        async def data_endpoint():
            return {"data": "content"}

        client = TestClient(app)

        # Regular user
        response1 = client.get("/api/data")
        assert response1.status_code == 402

        # Premium user
        response2 = client.get("/api/data", headers={"X-User-Type": "premium"})
        assert response2.status_code == 402

        # Verify different amounts
        calls = mock_x402_client.payment_required_response.call_args_list
        assert calls[0].kwargs["amount"] == Decimal("1.00")
        assert calls[1].kwargs["amount"] == Decimal("0.50")


# =============================================================================
# Payment State Tests
# =============================================================================


class TestPaymentState:
    """Test that payment info is added to request state."""

    def test_payment_info_in_request_state(self, mock_x402_client):
        """Valid payment should add info to request.state."""
        import base64
        import json

        from starlette.routing import Route
        from starlette.applications import Starlette

        mock_x402_client.verify_payment = AsyncMock(
            return_value=X402PaymentVerification(
                valid=True,
                tx_hash="0x" + "ab" * 32,
                amount=Decimal("1.00"),
                error=None,
            )
        )

        payment_info_received = {}

        async def data_endpoint(request):
            """Test endpoint that captures payment info from state."""
            payment_info_received.update(getattr(request.state, "x402_payment", {}))
            from starlette.responses import JSONResponse

            return JSONResponse({"data": "content"})

        # Use Starlette app directly to avoid FastAPI annotation resolution issues
        app = Starlette(routes=[Route("/api/data", data_endpoint, methods=["GET"])])
        app.add_middleware(
            X402PaymentMiddleware,
            x402_client=mock_x402_client,
            protected_paths={"/api/data": Decimal("1.00")},
        )

        client = TestClient(app)

        payment = base64.b64encode(json.dumps({"tx_hash": "0x" + "ab" * 32}).encode()).decode()

        response = client.get("/api/data", headers={"X-Payment": payment})

        assert response.status_code == 200
        assert payment_info_received.get("verified") is True
        assert payment_info_received.get("tx_hash") == "0x" + "ab" * 32
