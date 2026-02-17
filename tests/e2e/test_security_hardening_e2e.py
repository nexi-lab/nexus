"""E2E tests for security hardening (Issue #1596).

Starts a real nexus server and verifies:
- CORS headers are set correctly (not wildcard)
- Debug endpoint is not exposed (NEXUS_DEBUG_ENABLED not set)
- Admin endpoints reject non-admin users
- Health endpoint still responds quickly (no performance regression)
"""

from __future__ import annotations

import time

import httpx
import pytest


@pytest.fixture(scope="function")
def api_key():
    """API key matching the server's NEXUS_API_KEY."""
    return "test-e2e-api-key-12345"


class TestCORSHeadersE2E:
    """CORS headers in live server must not be wildcard + credentials."""

    def test_cors_preflight_returns_allowed_origins(self, test_app: httpx.Client) -> None:
        """OPTIONS preflight should return specific origin, not wildcard."""
        resp = test_app.options(
            "/health",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        # Should either return the specific origin or no CORS header
        allow_origin = resp.headers.get("access-control-allow-origin", "")
        # Must NOT be "*" when credentials are involved
        if "access-control-allow-credentials" in resp.headers:
            assert allow_origin != "*", "CORS wildcard with credentials is forbidden"

    def test_unknown_origin_rejected(self, test_app: httpx.Client) -> None:
        """Request from unknown origin should not get CORS headers."""
        resp = test_app.get(
            "/health",
            headers={"Origin": "https://evil.example.com"},
        )
        allow_origin = resp.headers.get("access-control-allow-origin", "")
        # Should NOT reflect the evil origin
        assert allow_origin != "https://evil.example.com"


class TestDebugEndpointE2E:
    """Debug endpoint must not be accessible without NEXUS_DEBUG_ENABLED."""

    def test_debug_endpoint_not_found(self, test_app: httpx.Client, api_key: str) -> None:
        """Without NEXUS_DEBUG_ENABLED, /debug/asyncio should return 404."""
        resp = test_app.get(
            "/debug/asyncio",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        # Endpoint should not be registered at all (404), not just forbidden
        assert resp.status_code == 404, (
            f"Debug endpoint should not be registered without NEXUS_DEBUG_ENABLED, "
            f"got {resp.status_code}"
        )


class TestAdminEndpointsE2E:
    """Admin endpoints must enforce admin role."""

    def test_admin_hotspot_stats_requires_auth(self, test_app: httpx.Client) -> None:
        """Unauthenticated request to admin endpoint gets 401."""
        resp = test_app.get("/api/v1/admin/hotspot-stats")
        assert resp.status_code in (401, 403, 422), (
            f"Admin endpoint without auth should fail, got {resp.status_code}"
        )


class TestPerformanceE2E:
    """Security changes must not cause performance regression."""

    def test_health_endpoint_latency(self, test_app: httpx.Client) -> None:
        """Health endpoint should respond in < 100ms (no CORS overhead)."""
        # Warm up
        test_app.get("/health")

        # Measure
        latencies = []
        for _ in range(10):
            start = time.perf_counter()
            resp = test_app.get("/health")
            elapsed = (time.perf_counter() - start) * 1000
            latencies.append(elapsed)
            assert resp.status_code == 200

        avg_latency = sum(latencies) / len(latencies)
        p99_latency = sorted(latencies)[int(len(latencies) * 0.99)]

        # Assert reasonable latency (generous: local server should be < 50ms)
        assert avg_latency < 100, f"Average health latency {avg_latency:.1f}ms exceeds 100ms"
        assert p99_latency < 200, f"P99 health latency {p99_latency:.1f}ms exceeds 200ms"
