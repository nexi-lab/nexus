#!/usr/bin/env python3
"""End-to-end test script for Query Router API (Issue #1041).

Tests the /api/search/query endpoint with graph_mode=auto parameter.

Usage:
    # Start the server first:
    NEXUS_SEARCH_DAEMON=true python -m nexus.cli.main serve

    # Then run the test:
    python scripts/test_query_router_api_e2e.py

    # Or specify a different server URL:
    python scripts/test_query_router_api_e2e.py --url http://localhost:2026
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class QueryRouterAPITest:
    """E2E test for query router API."""

    def __init__(self, base_url: str, api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("NEXUS_API_KEY", "test-key")
        self.passed = 0
        self.failed = 0

    def _check(self, name: str, condition: bool, message: str = ""):
        """Check a test condition and log result."""
        if condition:
            self.passed += 1
            logger.info(f"  ✓ {name}")
        else:
            self.failed += 1
            logger.error(f"  ✗ {name}: {message}")

    async def test_health_check(self, client: httpx.AsyncClient):
        """Test that the server is healthy."""
        logger.info("\n=== Test: Health Check ===")

        response = await client.get(f"{self.base_url}/health")
        self._check("Health endpoint returns 200", response.status_code == 200)

        if response.status_code == 200:
            data = response.json()
            self._check("Status is healthy", data.get("status") == "healthy")

    async def test_auto_mode_simple_query(self, client: httpx.AsyncClient):
        """Test auto mode routes simple queries correctly."""
        logger.info("\n=== Test: Auto Mode - Simple Query ===")

        response = await client.get(
            f"{self.base_url}/api/search/query",
            params={
                "q": "what is authentication",
                "graph_mode": "auto",
            },
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

        self._check("Request succeeds", response.status_code == 200)

        if response.status_code == 200:
            data = response.json()
            self._check("Response has routing info", "routing" in data)

            if "routing" in data:
                routing = data["routing"]
                self._check(
                    "Complexity class is simple",
                    routing.get("complexity_class") == "simple",
                    f"Got: {routing.get('complexity_class')}",
                )
                self._check(
                    "Graph mode is none for simple query",
                    routing.get("graph_mode") == "none",
                    f"Got: {routing.get('graph_mode')}",
                )
                self._check(
                    "Complexity score < 0.3",
                    routing.get("complexity_score", 1.0) < 0.3,
                    f"Got: {routing.get('complexity_score')}",
                )
                self._check(
                    "Routing latency recorded",
                    "routing_latency_ms" in routing,
                )
                self._check(
                    "Reasoning provided",
                    "reasoning" in routing and len(routing.get("reasoning", "")) > 0,
                )

    async def test_auto_mode_moderate_query(self, client: httpx.AsyncClient):
        """Test auto mode routes moderate queries correctly."""
        logger.info("\n=== Test: Auto Mode - Moderate Query ===")

        response = await client.get(
            f"{self.base_url}/api/search/query",
            params={
                "q": "compare OAuth vs JWT for API authentication",
                "graph_mode": "auto",
            },
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

        self._check("Request succeeds", response.status_code == 200)

        if response.status_code == 200:
            data = response.json()
            self._check("Response has routing info", "routing" in data)

            if "routing" in data:
                routing = data["routing"]
                # Moderate queries should have complexity 0.3-0.6
                complexity = routing.get("complexity_score", 0)
                self._check(
                    "Complexity in moderate range (0.3-0.6)",
                    0.3 <= complexity < 0.6,
                    f"Got: {complexity}",
                )
                self._check(
                    "Graph mode is low for moderate query",
                    routing.get("graph_mode") == "low",
                    f"Got: {routing.get('graph_mode')}",
                )

    async def test_auto_mode_complex_query(self, client: httpx.AsyncClient):
        """Test auto mode routes complex queries correctly."""
        logger.info("\n=== Test: Auto Mode - Complex Query ===")

        response = await client.get(
            f"{self.base_url}/api/search/query",
            params={
                "q": "How does the AuthService authenticate users and what happens when the JWTProvider expires the token before the refresh interval",
                "graph_mode": "auto",
            },
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

        self._check("Request succeeds", response.status_code == 200)

        if response.status_code == 200:
            data = response.json()
            self._check("Response has routing info", "routing" in data)

            if "routing" in data:
                routing = data["routing"]
                complexity = routing.get("complexity_score", 0)
                complexity_class = routing.get("complexity_class", "")

                self._check(
                    "Complexity class is complex or very_complex",
                    complexity_class in ("complex", "very_complex"),
                    f"Got: {complexity_class}",
                )
                self._check(
                    "Graph mode is dual for complex query",
                    routing.get("graph_mode") == "dual",
                    f"Got: {routing.get('graph_mode')}",
                )
                self._check(
                    "Complexity score >= 0.6",
                    complexity >= 0.6,
                    f"Got: {complexity}",
                )

    async def test_auto_mode_limit_adjustment(self, client: httpx.AsyncClient):
        """Test that auto mode adjusts limit correctly."""
        logger.info("\n=== Test: Auto Mode - Limit Adjustment ===")

        # Simple query should have limit * 0.8
        response = await client.get(
            f"{self.base_url}/api/search/query",
            params={
                "q": "test",
                "graph_mode": "auto",
                "limit": 10,
            },
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

        self._check("Request succeeds", response.status_code == 200)

        if response.status_code == 200:
            data = response.json()
            if "routing" in data:
                routing = data["routing"]
                adjusted_limit = routing.get("adjusted_limit", 0)
                complexity_class = routing.get("complexity_class", "")

                if complexity_class == "simple":
                    self._check(
                        "Simple query limit adjusted to 8 (10 * 0.8)",
                        adjusted_limit == 8,
                        f"Got: {adjusted_limit}",
                    )
                elif complexity_class == "moderate":
                    self._check(
                        "Moderate query limit unchanged (10 * 1.0)",
                        adjusted_limit == 10,
                        f"Got: {adjusted_limit}",
                    )

    async def test_invalid_graph_mode(self, client: httpx.AsyncClient):
        """Test that invalid graph_mode returns 400."""
        logger.info("\n=== Test: Invalid graph_mode ===")

        response = await client.get(
            f"{self.base_url}/api/search/query",
            params={
                "q": "test",
                "graph_mode": "invalid_mode",
            },
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

        self._check("Invalid mode returns 400", response.status_code == 400)

        if response.status_code == 400:
            data = response.json()
            self._check(
                "Error mentions valid options including 'auto'",
                "auto" in str(data.get("detail", "")),
            )

    async def test_explicit_modes_still_work(self, client: httpx.AsyncClient):
        """Test that explicit graph modes still work (no routing)."""
        logger.info("\n=== Test: Explicit Modes (No Routing) ===")

        for mode in ["none", "low", "dual"]:
            response = await client.get(
                f"{self.base_url}/api/search/query",
                params={
                    "q": "test query",
                    "graph_mode": mode,
                },
                headers={"Authorization": f"Bearer {self.api_key}"},
            )

            self._check(f"Mode '{mode}' returns 200", response.status_code == 200)

            if response.status_code == 200:
                data = response.json()
                # Explicit modes should NOT have routing info
                has_routing = "routing" in data
                self._check(
                    f"Mode '{mode}' has no routing info (explicit mode)",
                    not has_routing,
                    "Unexpected routing info present",
                )
                self._check(
                    f"Mode '{mode}' uses requested graph_mode",
                    data.get("graph_mode") == mode,
                    f"Got: {data.get('graph_mode')}",
                )

    async def test_routing_performance(self, client: httpx.AsyncClient):
        """Test routing performance is under 5ms."""
        logger.info("\n=== Test: Routing Performance ===")

        latencies = []
        for i in range(5):
            response = await client.get(
                f"{self.base_url}/api/search/query",
                params={
                    "q": f"How does authentication work in scenario {i}",
                    "graph_mode": "auto",
                },
                headers={"Authorization": f"Bearer {self.api_key}"},
            )

            if response.status_code == 200:
                data = response.json()
                if "routing" in data:
                    latency = data["routing"].get("routing_latency_ms", 0)
                    latencies.append(latency)

        if latencies:
            avg_latency = sum(latencies) / len(latencies)
            max_latency = max(latencies)

            logger.info(f"  Routing latencies: {[f'{lat:.3f}ms' for lat in latencies]}")
            logger.info(f"  Average: {avg_latency:.3f}ms, Max: {max_latency:.3f}ms")

            self._check(
                "Average routing latency < 5ms",
                avg_latency < 5.0,
                f"Got: {avg_latency:.3f}ms",
            )
            self._check(
                "Max routing latency < 10ms",
                max_latency < 10.0,
                f"Got: {max_latency:.3f}ms",
            )

    async def run_all_tests(self):
        """Run all E2E tests."""
        logger.info("=" * 60)
        logger.info("Query Router API E2E Tests (Issue #1041)")
        logger.info(f"Base URL: {self.base_url}")
        logger.info("=" * 60)

        # Disable proxy to connect directly to localhost
        transport = httpx.AsyncHTTPTransport(proxy=None)
        async with httpx.AsyncClient(timeout=30.0, transport=transport) as client:
            await self.test_health_check(client)
            await self.test_auto_mode_simple_query(client)
            await self.test_auto_mode_moderate_query(client)
            await self.test_auto_mode_complex_query(client)
            await self.test_auto_mode_limit_adjustment(client)
            await self.test_invalid_graph_mode(client)
            await self.test_explicit_modes_still_work(client)
            await self.test_routing_performance(client)

        logger.info("\n" + "=" * 60)
        logger.info(f"Results: {self.passed} passed, {self.failed} failed")
        logger.info("=" * 60)

        return self.failed == 0


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Query Router API E2E Tests")
    parser.add_argument(
        "--url",
        default=os.environ.get("NEXUS_SERVER_URL", "http://localhost:2027"),
        help="Server URL (default: http://localhost:2027)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("NEXUS_API_KEY"),
        help="API key for authentication",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    test = QueryRouterAPITest(base_url=args.url, api_key=args.api_key)
    success = await test.run_all_tests()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
