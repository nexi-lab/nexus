#!/usr/bin/env python3
"""Performance benchmark for x402 protocol implementation.

Tests:
1. Payment required response generation
2. Payment header parsing/verification
3. Middleware path matching
4. Webhook signature verification
5. Full request flow throughput

Usage:
    uv run python scripts/benchmark_x402.py
"""

import base64
import json
import os
import sys
import time
from decimal import Decimal

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.pay.x402 import (
    X402Client,
    X402PaymentVerification,
    micro_to_usdc,
    usdc_to_micro,
    validate_wallet_address,
)
from nexus.server.middleware.x402 import X402PaymentMiddleware


def benchmark(name: str, iterations: int = 10000):
    """Decorator to benchmark a function."""

    def decorator(func):
        def wrapper(*args, **kwargs):
            # Warmup
            for _ in range(100):
                func(*args, **kwargs)

            # Benchmark
            start = time.perf_counter()
            for _ in range(iterations):
                func(*args, **kwargs)
            elapsed = time.perf_counter() - start

            ops_per_sec = iterations / elapsed
            us_per_op = (elapsed / iterations) * 1_000_000

            print(f"  {name}:")
            print(f"    {iterations:,} iterations in {elapsed:.3f}s")
            print(f"    {ops_per_sec:,.0f} ops/sec | {us_per_op:.2f} µs/op")
            return ops_per_sec

        return wrapper

    return decorator


def run_benchmarks():
    """Run all benchmarks."""
    print("\n" + "=" * 60)
    print("x402 Performance Benchmarks")
    print("=" * 60)

    # Setup
    x402_client = X402Client(
        facilitator_url="https://x402.org/facilitator",
        wallet_address="0x1234567890123456789012345678901234567890",
        network="base",
        webhook_secret="benchmark-secret-key",
    )

    # =========================================================================
    # 1. USDC Conversion Benchmarks
    # =========================================================================
    print("\n[1] USDC Conversion Operations")

    @benchmark("usdc_to_micro", 100000)
    def bench_usdc_to_micro():
        usdc_to_micro(Decimal("123.456789"))

    @benchmark("micro_to_usdc", 100000)
    def bench_micro_to_usdc():
        micro_to_usdc(123456789)

    bench_usdc_to_micro()
    bench_micro_to_usdc()

    # =========================================================================
    # 2. Wallet Address Validation
    # =========================================================================
    print("\n[2] Wallet Address Validation")

    @benchmark("validate_wallet_address (valid)", 100000)
    def bench_validate_valid():
        validate_wallet_address("0x1234567890123456789012345678901234567890")

    @benchmark("validate_wallet_address (invalid)", 100000)
    def bench_validate_invalid():
        validate_wallet_address("invalid-address")

    bench_validate_valid()
    bench_validate_invalid()

    # =========================================================================
    # 3. Payment Required Response Generation
    # =========================================================================
    print("\n[3] Payment Required Response Generation")

    @benchmark("payment_required_response", 10000)
    def bench_payment_required():
        x402_client.payment_required_response(
            amount=Decimal("1.00"),
            description="API access",
        )

    bench_payment_required()

    # =========================================================================
    # 4. Payment Header Encoding/Decoding
    # =========================================================================
    print("\n[4] Payment Header Encoding/Decoding")

    payload = {
        "tx_hash": "0x" + "ab" * 32,
        "amount": "1000000",
        "network": "eip155:8453",
        "signature": "0x" + "cd" * 65,
    }
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()

    @benchmark("encode payment header", 50000)
    def bench_encode():
        base64.b64encode(json.dumps(payload).encode()).decode()

    @benchmark("decode payment header", 50000)
    def bench_decode():
        json.loads(base64.b64decode(encoded).decode())

    bench_encode()
    bench_decode()

    # =========================================================================
    # 5. Webhook Signature Verification (HMAC-SHA256)
    # =========================================================================
    print("\n[5] Webhook Signature Verification")

    webhook_payload = {
        "event": "payment.confirmed",
        "tx_hash": "0x" + "ef" * 32,
        "amount": "1000000",
        "metadata": {"agent_id": "agent-123"},
    }

    @benchmark("HMAC-SHA256 signature verify", 50000)
    def bench_hmac():
        x402_client._verify_webhook_signature(webhook_payload)

    bench_hmac()

    # =========================================================================
    # 6. Full Request Throughput (TestClient)
    # =========================================================================
    print("\n[6] Full Request Throughput")

    # Create test app
    app = FastAPI()

    # Mock verify to be instant
    async def instant_verify(payment_header, expected_amount):
        return X402PaymentVerification(
            valid=True,
            tx_hash="0x" + "ab" * 32,
            amount=expected_amount,
            error=None,
        )

    x402_client.verify_payment = instant_verify

    app.add_middleware(
        X402PaymentMiddleware,
        x402_client=x402_client,
        protected_paths={
            "/api/premium": Decimal("1.00"),
        },
    )

    @app.get("/api/free")
    async def free():
        return {"ok": True}

    @app.get("/api/premium")
    async def premium():
        return {"ok": True}

    client = TestClient(app)
    payment = base64.b64encode(json.dumps({"valid": True}).encode()).decode()

    @benchmark("free endpoint (no middleware)", 5000)
    def bench_free():
        client.get("/api/free")

    @benchmark("protected endpoint (402 response)", 5000)
    def bench_402():
        client.get("/api/premium")

    @benchmark("protected endpoint (with payment)", 5000)
    def bench_paid():
        client.get("/api/premium", headers={"X-Payment": payment})

    free_ops = bench_free()
    ops_402 = bench_402()
    paid_ops = bench_paid()

    # =========================================================================
    # 7. Middleware Path Matching
    # =========================================================================
    print("\n[7] Middleware Path Matching Comparison")

    # Current implementation: dict prefix scan
    protected_paths = {
        "/api/v2/premium": Decimal("1.00"),
        "/api/v2/data": Decimal("0.50"),
        "/api/v2/expensive": Decimal("10.00"),
        "/api/v2/reports": Decimal("5.00"),
        "/api/v2/analytics": Decimal("2.00"),
    }

    @benchmark("dict prefix scan (5 paths)", 100000)
    def bench_prefix_scan():
        path = "/api/v2/premium"
        for prefix, price in protected_paths.items():
            if path.startswith(prefix):
                return price
        return None

    # Optimized: set lookup for exact match
    protected_set = set(protected_paths.keys())

    @benchmark("set lookup (exact match)", 100000)
    def bench_set_lookup():
        path = "/api/v2/premium"
        if path in protected_set:
            return protected_paths[path]
        return None

    bench_prefix_scan()
    bench_set_lookup()

    # =========================================================================
    # Summary
    # =========================================================================
    print("\n" + "=" * 60)
    print("PERFORMANCE SUMMARY")
    print("=" * 60)
    print(f"""
Key Metrics:
  • Free endpoint:        {free_ops:,.0f} req/sec
  • Protected (402):      {ops_402:,.0f} req/sec
  • Protected (paid):     {paid_ops:,.0f} req/sec

Bottlenecks Identified:
  1. HTTP call to facilitator /verify (not benchmarked - network bound)
  2. JSON encoding/decoding for headers
  3. Middleware prefix matching (O(n) for n protected paths)

Optimization Recommendations:
  1. Cache verified payments (5-60 second TTL)
  2. Use connection pooling for facilitator calls
  3. Use exact path matching (set) instead of prefix scan
  4. Pre-encode static payment required responses
  5. Use orjson for faster JSON serialization
""")


if __name__ == "__main__":
    run_benchmarks()
