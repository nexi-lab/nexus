"""Performance smoke tests for Exchange Protocol changes (Issue #1361).

Verifies no latency regression from the models.py split or error handler registration.
Each endpoint should respond within reasonable thresholds.
"""

from __future__ import annotations

import statistics
import time

import httpx

AUTH_HEADERS = {"Authorization": "Bearer test-e2e-api-key-12345"}

# Thresholds in seconds — generous for cold SQLite on CI
THRESHOLD_FAST = 0.5  # Simple reads
THRESHOLD_MEDIUM = 1.0  # Writes / searches
THRESHOLD_SLOW = 2.0  # Complex operations


def _measure(client: httpx.Client, method: str, url: str, rounds: int = 5, **kwargs) -> dict:
    """Hit an endpoint multiple times and return timing stats."""
    times = []
    last_status = 0
    for _ in range(rounds):
        start = time.perf_counter()
        if method == "GET":
            resp = client.get(url, headers=AUTH_HEADERS, **kwargs)
        else:
            resp = client.post(url, headers=AUTH_HEADERS, **kwargs)
        elapsed = time.perf_counter() - start
        times.append(elapsed)
        last_status = resp.status_code
    return {
        "p50": statistics.median(times),
        "p95": sorted(times)[min(len(times) - 1, int(len(times) * 0.95))],
        "mean": statistics.mean(times),
        "min": min(times),
        "max": max(times),
        "status": last_status,
    }


class TestEndpointLatency:
    """Measure response times for key endpoints after models split."""

    def test_health_latency(self, test_app: httpx.Client) -> None:
        """Health endpoint baseline — should be <100ms."""
        stats = _measure(test_app, "GET", "/health", rounds=10)
        print(f"\n  /health: p50={stats['p50'] * 1000:.0f}ms p95={stats['p95'] * 1000:.0f}ms")
        assert stats["p50"] < THRESHOLD_FAST

    def test_memory_store_latency(self, test_app: httpx.Client) -> None:
        """Memory store — validates models/memory.py import path."""
        stats = _measure(
            test_app,
            "POST",
            "/api/v2/memories",
            json={"content": "perf test memory", "scope": "user"},
        )
        print(
            f"\n  POST /memories: p50={stats['p50'] * 1000:.0f}ms p95={stats['p95'] * 1000:.0f}ms"
        )
        assert stats["status"] in (200, 201)
        assert stats["p50"] < THRESHOLD_MEDIUM

    def test_memory_search_latency(self, test_app: httpx.Client) -> None:
        """Memory search — validates models/memory.py import path."""
        stats = _measure(
            test_app,
            "POST",
            "/api/v2/memories/search",
            json={"query": "perf test", "limit": 10},
        )
        print(
            f"\n  POST /memories/search: p50={stats['p50'] * 1000:.0f}ms p95={stats['p95'] * 1000:.0f}ms"
        )
        assert stats["status"] == 200
        assert stats["p50"] < THRESHOLD_MEDIUM

    def test_operations_list_latency(self, test_app: httpx.Client) -> None:
        """Operations list — validates models/operation.py import path."""
        stats = _measure(test_app, "GET", "/api/v2/operations?limit=10")
        print(
            f"\n  GET /operations: p50={stats['p50'] * 1000:.0f}ms p95={stats['p95'] * 1000:.0f}ms"
        )
        assert stats["status"] == 200
        assert stats["p50"] < THRESHOLD_FAST

    def test_audit_list_latency(self, test_app: httpx.Client) -> None:
        """Audit list — validates models/audit.py import path."""
        stats = _measure(test_app, "GET", "/api/v2/audit/transactions?limit=10")
        print(
            f"\n  GET /audit/transactions: p50={stats['p50'] * 1000:.0f}ms p95={stats['p95'] * 1000:.0f}ms"
        )
        assert stats["status"] == 200
        assert stats["p50"] < THRESHOLD_FAST

    def test_audit_aggregations_latency(self, test_app: httpx.Client) -> None:
        """Audit aggregations — validates models/audit.py import path."""
        stats = _measure(test_app, "GET", "/api/v2/audit/transactions/aggregations")
        print(
            f"\n  GET /audit/aggregations: p50={stats['p50'] * 1000:.0f}ms p95={stats['p95'] * 1000:.0f}ms"
        )
        assert stats["status"] == 200
        assert stats["p50"] < THRESHOLD_FAST

    def test_trajectory_start_latency(self, test_app: httpx.Client) -> None:
        """Trajectory start — validates models/trajectory.py import path."""
        stats = _measure(
            test_app,
            "POST",
            "/api/v2/trajectories",
            json={"task_description": "perf test", "task_type": "test"},
        )
        print(
            f"\n  POST /trajectories: p50={stats['p50'] * 1000:.0f}ms p95={stats['p95'] * 1000:.0f}ms"
        )
        assert stats["status"] in (200, 201)
        assert stats["p50"] < THRESHOLD_MEDIUM

    def test_consolidation_latency(self, test_app: httpx.Client) -> None:
        """Consolidation — validates models/consolidation.py import path."""
        stats = _measure(
            test_app,
            "POST",
            "/api/v2/consolidate",
            json={"beta": 0.7, "lambda_decay": 0.1, "limit": 10},
        )
        print(
            f"\n  POST /consolidate: p50={stats['p50'] * 1000:.0f}ms p95={stats['p95'] * 1000:.0f}ms"
        )
        assert stats["p50"] < THRESHOLD_SLOW

    def test_error_response_latency(self, test_app: httpx.Client) -> None:
        """Error path — verify error handler doesn't add overhead."""
        stats = _measure(
            test_app,
            "POST",
            "/api/v2/memories/search",
            json={},  # Triggers 422
        )
        print(
            f"\n  422 error path: p50={stats['p50'] * 1000:.0f}ms p95={stats['p95'] * 1000:.0f}ms"
        )
        assert stats["status"] == 422
        assert stats["p50"] < THRESHOLD_FAST
