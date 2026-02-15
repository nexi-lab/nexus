"""E2E test: resiliency health endpoint (Issue #1366).

Verifies that /health/detailed includes the resiliency component
when the server is running with services enabled, and that there
is no performance regression.
"""

from __future__ import annotations

import time

import httpx


class TestResiliencyE2E:
    def test_health_detailed_includes_resiliency(self, test_app: httpx.Client) -> None:
        """Fresh server should report resiliency component as 'ok'."""
        resp = test_app.get("/health/detailed")
        assert resp.status_code == 200

        health = resp.json()
        components = health.get("components", {})

        # Resiliency component must be present when services are wired
        assert "resiliency" in components, (
            f"Missing 'resiliency' in health components: {list(components)}"
        )
        resiliency = components["resiliency"]
        assert resiliency["status"] == "ok"
        assert "circuit_breakers" in resiliency

    def test_health_overall_not_degraded_on_fresh_start(self, test_app: httpx.Client) -> None:
        """A freshly started server should not have degraded resiliency."""
        resp = test_app.get("/health/detailed")
        assert resp.status_code == 200

        health = resp.json()
        resiliency = health["components"]["resiliency"]
        assert resiliency["status"] == "ok"

    def test_health_endpoint_performance(self, test_app: httpx.Client) -> None:
        """Verify /health/detailed responds within 100ms (no perf regression)."""
        # Warm-up request
        test_app.get("/health/detailed")

        # Benchmark 10 requests
        latencies: list[float] = []
        for _ in range(10):
            start = time.monotonic()
            resp = test_app.get("/health/detailed")
            elapsed_ms = (time.monotonic() - start) * 1000
            assert resp.status_code == 200
            latencies.append(elapsed_ms)

        avg_ms = sum(latencies) / len(latencies)
        p99_ms = sorted(latencies)[9]  # 10th value = p99 for 10 samples
        assert avg_ms < 100, f"Average latency {avg_ms:.1f}ms exceeds 100ms"
        assert p99_ms < 200, f"P99 latency {p99_ms:.1f}ms exceeds 200ms"
