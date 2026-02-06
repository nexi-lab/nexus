#!/usr/bin/env python3
"""End-to-end test script for x402 API endpoints.

This script tests the x402 integration using a real FastAPI TestClient
with actual HTTP request/response handling.

Usage:
    uv run python scripts/test_x402_e2e.py
"""

import base64
import json
import os
import sys
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.pay.x402 import X402Client, X402PaymentVerification
from nexus.server.api.v2.routers.x402 import router as x402_router
from nexus.server.middleware.x402 import X402PaymentMiddleware


def create_test_app():
    """Create a test FastAPI app with x402 integration."""
    app = FastAPI(title="x402 E2E Test Server")

    # Create real X402Client
    x402_client = X402Client(
        facilitator_url="https://x402.org/facilitator",
        wallet_address="0x1234567890123456789012345678901234567890",
        network="base",
        webhook_secret="e2e-test-secret",
    )

    # Mock the verify_payment to simulate facilitator response
    async def mock_verify(payment_header: str, expected_amount: Decimal):
        try:
            decoded = base64.b64decode(payment_header).decode()
            data = json.loads(decoded)
            if data.get("valid") is True:
                return X402PaymentVerification(
                    valid=True,
                    tx_hash=data.get("tx_hash", "0x" + "ab" * 32),
                    amount=expected_amount,
                    error=None,
                )
        except Exception:
            pass
        return X402PaymentVerification(
            valid=False, tx_hash=None, amount=None, error="Invalid payment"
        )

    x402_client.verify_payment = mock_verify

    # Mock webhook signature verification
    x402_client._verify_webhook_signature = MagicMock(return_value=True)

    # Mock credits service
    mock_credits = AsyncMock()
    mock_credits.topup = AsyncMock(return_value="tx-e2e-12345")
    mock_credits.provision_wallet = AsyncMock()

    # Add x402 router
    app.include_router(x402_router, prefix="/api/v2")

    # Add middleware for protected endpoints
    app.add_middleware(
        X402PaymentMiddleware,
        x402_client=x402_client,
        protected_paths={
            "/api/v2/premium": Decimal("1.00"),
            "/api/v2/data": Decimal("0.50"),
        },
    )

    # Set app state
    app.state.x402_client = x402_client
    app.state.credits_service = mock_credits

    # Add test endpoints
    @app.get("/api/v2/health")
    async def health():
        return {"status": "ok", "service": "x402-e2e-test"}

    @app.get("/api/v2/free")
    async def free_endpoint():
        return {"status": "ok", "data": "free content", "paid": False}

    @app.get("/api/v2/premium")
    async def premium_endpoint():
        return {"status": "ok", "data": "premium content", "paid": True, "price": "1.00"}

    @app.get("/api/v2/data")
    async def data_endpoint():
        return {"status": "ok", "data": "paid data", "paid": True, "price": "0.50"}

    return app


def run_e2e_tests():
    """Run all e2e tests."""
    print("\n" + "=" * 60)
    print("x402 End-to-End Tests (Real HTTP via TestClient)")
    print("=" * 60)

    app = create_test_app()
    client = TestClient(app)

    passed = 0
    failed = 0

    # Test 1: Health check
    print("\n[TEST 1] Health check endpoint...")
    try:
        r = client.get("/api/v2/health")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}"
        assert r.json()["status"] == "ok"
        print(f"  ✅ PASSED - Status: {r.status_code}, Response: {r.json()}")
        passed += 1
    except Exception as e:
        print(f"  ❌ FAILED - {type(e).__name__}: {e}")
        failed += 1

    # Test 2: Free endpoint (no payment needed)
    print("\n[TEST 2] Free endpoint (no payment)...")
    try:
        r = client.get("/api/v2/free")
        assert r.status_code == 200
        assert r.json()["paid"] is False
        print(f"  ✅ PASSED - Status: {r.status_code}, Response: {r.json()}")
        passed += 1
    except Exception as e:
        print(f"  ❌ FAILED - {type(e).__name__}: {e}")
        failed += 1

    # Test 3: x402 Config endpoint
    print("\n[TEST 3] x402 config endpoint...")
    try:
        r = client.get("/api/v2/x402/config")
        assert r.status_code == 200
        data = r.json()
        assert data["enabled"] is True
        assert data["network"] == "base"
        assert "wallet_address" in data
        print(f"  ✅ PASSED - Status: {r.status_code}")
        print(f"     Config: enabled={data['enabled']}, network={data['network']}")
        print(f"     Wallet: {data['wallet_address']}")
        passed += 1
    except Exception as e:
        print(f"  ❌ FAILED - {type(e).__name__}: {e}")
        failed += 1

    # Test 4: Premium endpoint WITHOUT payment (should return 402)
    print("\n[TEST 4] Premium endpoint WITHOUT payment...")
    try:
        r = client.get("/api/v2/premium")
        assert r.status_code == 402, f"Expected 402, got {r.status_code}"
        assert "X-Payment-Required" in r.headers
        header = r.headers["X-Payment-Required"]
        payload = json.loads(base64.b64decode(header).decode())
        assert payload["amount"] == "1.00"
        assert payload["currency"] == "USDC"
        print(f"  ✅ PASSED - Status: {r.status_code} (Payment Required)")
        print(f"     Amount: {payload['amount']} {payload['currency']}")
        print(f"     Network: {payload['network']}")
        print(f"     Address: {payload['address']}")
        passed += 1
    except Exception as e:
        print(f"  ❌ FAILED - {type(e).__name__}: {e}")
        failed += 1

    # Test 5: Premium endpoint WITH valid payment
    print("\n[TEST 5] Premium endpoint WITH valid payment...")
    try:
        payment = base64.b64encode(
            json.dumps({"valid": True, "tx_hash": "0x" + "cd" * 32}).encode()
        ).decode()
        r = client.get("/api/v2/premium", headers={"X-Payment": payment})
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        data = r.json()
        assert data["paid"] is True
        print(f"  ✅ PASSED - Status: {r.status_code}")
        print(f"     Response: {data}")
        passed += 1
    except Exception as e:
        print(f"  ❌ FAILED - {type(e).__name__}: {e}")
        failed += 1

    # Test 6: Premium endpoint WITH invalid payment
    print("\n[TEST 6] Premium endpoint WITH invalid payment...")
    try:
        payment = base64.b64encode(
            json.dumps({"valid": False, "tx_hash": "invalid"}).encode()
        ).decode()
        r = client.get("/api/v2/premium", headers={"X-Payment": payment})
        assert r.status_code == 402
        print(f"  ✅ PASSED - Status: {r.status_code} (Payment rejected)")
        print(f"     Error: {r.json()}")
        passed += 1
    except Exception as e:
        print(f"  ❌ FAILED - {type(e).__name__}: {e}")
        failed += 1

    # Test 7: Topup endpoint (should return 402 with payment details)
    print("\n[TEST 7] Topup endpoint...")
    try:
        r = client.post(
            "/api/v2/x402/topup",
            json={"agent_id": "agent-e2e-test", "amount": "25.00"},
        )
        assert r.status_code == 402
        data = r.json()
        assert data["payment_required"] is True
        assert data["amount"] == "25.00"
        print(f"  ✅ PASSED - Status: {r.status_code}")
        print(f"     Payment Required: {data['amount']} {data['currency']}")
        print(f"     Pay to: {data['address']}")
        passed += 1
    except Exception as e:
        print(f"  ❌ FAILED - {type(e).__name__}: {e}")
        failed += 1

    # Test 8: Webhook endpoint (payment confirmation)
    print("\n[TEST 8] Webhook endpoint (payment confirmation)...")
    try:
        webhook_payload = {
            "event": "payment.confirmed",
            "tx_hash": "0x" + "ef" * 32,
            "network": "eip155:8453",
            "amount": "25000000",  # 25 USDC
            "currency": "USDC",
            "from": "0xbuyer1234567890buyer1234567890buyer12",
            "to": "0x1234567890123456789012345678901234567890",
            "timestamp": "2025-02-05T12:00:00Z",
            "metadata": {"agent_id": "agent-e2e-test", "tenant_id": "default"},
            "signature": "valid-e2e-signature",
        }
        r = client.post("/api/v2/x402/webhook", json=webhook_payload)
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        data = r.json()
        assert data["status"] == "credited"
        print(f"  ✅ PASSED - Status: {r.status_code}")
        print(f"     Credits Status: {data['status']}")
        print(f"     Transaction ID: {data['tx_id']}")
        passed += 1
    except Exception as e:
        print(f"  ❌ FAILED - {type(e).__name__}: {e}")
        failed += 1

    # Test 9: Webhook with missing agent_id
    print("\n[TEST 9] Webhook with missing agent_id...")
    try:
        bad_webhook = {
            "event": "payment.confirmed",
            "tx_hash": "0x" + "ab" * 32,
            "network": "eip155:8453",
            "amount": "1000000",
            "currency": "USDC",
            "from": "0xbuyer1234567890buyer1234567890buyer12",
            "to": "0x1234567890123456789012345678901234567890",
            "timestamp": "2025-02-05T12:00:00Z",
            "metadata": {},  # Missing agent_id
            "signature": "sig",
        }
        r = client.post("/api/v2/x402/webhook", json=bad_webhook)
        assert r.status_code == 400
        print(f"  ✅ PASSED - Status: {r.status_code} (Correctly rejected)")
        print(f"     Error: {r.json()['detail']}")
        passed += 1
    except Exception as e:
        print(f"  ❌ FAILED - {type(e).__name__}: {e}")
        failed += 1

    # Test 10: Different price endpoint (data - $0.50)
    print("\n[TEST 10] Data endpoint (different price - $0.50)...")
    try:
        r = client.get("/api/v2/data")
        assert r.status_code == 402
        header = r.headers["X-Payment-Required"]
        payload = json.loads(base64.b64decode(header).decode())
        assert payload["amount"] == "0.50"
        print(f"  ✅ PASSED - Status: {r.status_code}")
        print(f"     Price: {payload['amount']} {payload['currency']}")
        passed += 1
    except Exception as e:
        print(f"  ❌ FAILED - {type(e).__name__}: {e}")
        failed += 1

    # Test 11: Full flow - topup request then webhook
    print("\n[TEST 11] Full flow - topup then webhook...")
    try:
        # Step 1: Request topup
        topup_r = client.post(
            "/api/v2/x402/topup",
            json={"agent_id": "agent-flow-test", "amount": "100.00"},
        )
        assert topup_r.status_code == 402
        topup_data = topup_r.json()

        # Step 2: Simulate payment webhook
        webhook_r = client.post(
            "/api/v2/x402/webhook",
            json={
                "event": "payment.confirmed",
                "tx_hash": "0x" + "11" * 32,
                "network": "eip155:8453",
                "amount": "100000000",
                "currency": "USDC",
                "from": "0xbuyer1234567890buyer1234567890buyer12",
                "to": topup_data["address"],
                "timestamp": "2025-02-05T15:00:00Z",
                "metadata": {"agent_id": "agent-flow-test", "tenant_id": "default"},
                "signature": "flow-test-sig",
            },
        )
        assert webhook_r.status_code == 200
        webhook_data = webhook_r.json()
        assert webhook_data["status"] == "credited"

        print("  ✅ PASSED - Full flow completed")
        print(f"     Step 1: Topup requested for {topup_data['amount']} USDC")
        print(f"     Step 2: Payment confirmed, tx_id={webhook_data['tx_id']}")
        passed += 1
    except Exception as e:
        print(f"  ❌ FAILED - {type(e).__name__}: {e}")
        failed += 1

    # Summary
    print("\n" + "=" * 60)
    print(f"RESULTS: {passed} passed, {failed} failed out of 11 tests")
    print("=" * 60)

    return failed == 0


def main():
    """Main entry point."""
    success = run_e2e_tests()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
