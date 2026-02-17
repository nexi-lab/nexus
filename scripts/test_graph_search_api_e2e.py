#!/usr/bin/env python3
"""End-to-end test script for Graph-Enhanced Search API (Issue #1040).

Tests the /api/search/query endpoint with graph_mode parameter.

Usage:
    # Start the server first:
    NEXUS_SEARCH_DAEMON=true python -m nexus.cli.main server start

    # Then run the test:
    python scripts/test_graph_search_api_e2e.py

    # Or specify a different server URL:
    python scripts/test_graph_search_api_e2e.py --url http://localhost:2026

References:
    - https://github.com/nexi-lab/nexus/issues/1040
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class GraphSearchAPITest:
    """E2E test for graph-enhanced search API."""

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

    async def setup_test_data(self, client: httpx.AsyncClient):
        """Create test entities for graph-enhanced search."""
        logger.info("Setting up test data...")

        # Create some test entities via the graph API
        entities = [
            {"name": "AuthService", "entity_type": "CLASS"},
            {"name": "JWTProvider", "entity_type": "CLASS"},
            {"name": "UserRepository", "entity_type": "CLASS"},
        ]

        for entity in entities:
            try:
                # This endpoint might not exist, but we try anyway
                response = await client.post(
                    f"{self.base_url}/api/graph/entity",
                    json=entity,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                if response.status_code in (200, 201):
                    logger.debug(f"Created entity: {entity['name']}")
            except Exception as e:
                logger.debug(f"Could not create entity (expected): {e}")

    async def test_health_check(self, client: httpx.AsyncClient):
        """Test that the server is healthy."""
        logger.info("\n=== Test: Health Check ===")

        response = await client.get(f"{self.base_url}/health")
        self._check("Health endpoint returns 200", response.status_code == 200)

        if response.status_code == 200:
            data = response.json()
            self._check("Status is healthy", data.get("status") == "healthy")

    async def test_search_with_graph_mode_none(self, client: httpx.AsyncClient):
        """Test search with graph_mode=none (default)."""
        logger.info("\n=== Test: Search with graph_mode=none ===")

        response = await client.get(
            f"{self.base_url}/api/search/query",
            params={
                "q": "authentication",
                "type": "hybrid",
                "limit": 5,
                "graph_mode": "none",
            },
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

        self._check("Request succeeds", response.status_code == 200)

        if response.status_code == 200:
            data = response.json()
            self._check("Response has query", data.get("query") == "authentication")
            self._check("Response has graph_mode", data.get("graph_mode") == "none")
            self._check("Response has results array", isinstance(data.get("results"), list))
            self._check("Response has latency_ms", "latency_ms" in data)

            # Results should NOT have graph_score or graph_context in mode=none
            if data.get("results"):
                first_result = data["results"][0]
                self._check("Result has path", "path" in first_result)
                self._check("Result has score", "score" in first_result)

    async def test_search_with_graph_mode_low(self, client: httpx.AsyncClient):
        """Test search with graph_mode=low (entity-based)."""
        logger.info("\n=== Test: Search with graph_mode=low ===")

        response = await client.get(
            f"{self.base_url}/api/search/query",
            params={
                "q": "user authentication service",
                "type": "hybrid",
                "limit": 5,
                "graph_mode": "low",
            },
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

        self._check("Request succeeds", response.status_code == 200)

        if response.status_code == 200:
            data = response.json()
            self._check("Response has graph_mode=low", data.get("graph_mode") == "low")
            self._check("Response has results array", isinstance(data.get("results"), list))

            # In graph mode, results should have graph_score field
            if data.get("results"):
                first_result = data["results"][0]
                self._check("Result has graph_score field", "graph_score" in first_result)
                self._check("Result has graph_context field", "graph_context" in first_result)

    async def test_search_with_graph_mode_dual(self, client: httpx.AsyncClient):
        """Test search with graph_mode=dual (full LightRAG)."""
        logger.info("\n=== Test: Search with graph_mode=dual ===")

        response = await client.get(
            f"{self.base_url}/api/search/query",
            params={
                "q": "how does authentication work",
                "type": "hybrid",
                "limit": 10,
                "graph_mode": "dual",
            },
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

        self._check("Request succeeds", response.status_code == 200)

        if response.status_code == 200:
            data = response.json()
            self._check("Response has graph_mode=dual", data.get("graph_mode") == "dual")
            self._check("Response has latency_ms", "latency_ms" in data)

            logger.info(f"  Latency: {data.get('latency_ms', 'N/A')}ms")
            logger.info(f"  Results: {data.get('total', 0)}")

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
            self._check("Error mentions graph_mode", "graph_mode" in str(data.get("detail", "")))

    async def test_search_performance(self, client: httpx.AsyncClient):
        """Test search performance with graph_mode."""
        logger.info("\n=== Test: Performance ===")

        # Warm up
        await client.get(
            f"{self.base_url}/api/search/query",
            params={"q": "test", "graph_mode": "none"},
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

        # Test different modes
        modes = ["none", "low"]
        for mode in modes:
            latencies = []
            for i in range(3):
                start = time.perf_counter()
                response = await client.get(
                    f"{self.base_url}/api/search/query",
                    params={
                        "q": f"authentication test {i}",
                        "graph_mode": mode,
                        "limit": 10,
                    },
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                latency = (time.perf_counter() - start) * 1000
                latencies.append(latency)

                if response.status_code == 200:
                    server_latency = response.json().get("latency_ms", 0)
                    logger.debug(f"  {mode}: client={latency:.1f}ms, server={server_latency:.1f}ms")

            avg_latency = sum(latencies) / len(latencies)
            logger.info(f"  Mode '{mode}': avg={avg_latency:.1f}ms")
            self._check(f"Mode '{mode}' latency < 2000ms", avg_latency < 2000)

    async def run_all_tests(self):
        """Run all E2E tests."""
        logger.info("=" * 60)
        logger.info("Graph-Enhanced Search API E2E Tests (Issue #1040)")
        logger.info(f"Base URL: {self.base_url}")
        logger.info("=" * 60)

        # Disable proxy to connect directly to localhost
        transport = httpx.AsyncHTTPTransport(proxy=None)
        async with httpx.AsyncClient(timeout=30.0, transport=transport) as client:
            await self.test_health_check(client)

            # Check if search daemon is enabled
            try:
                response = await client.get(
                    f"{self.base_url}/api/search/health",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                if response.status_code != 200:
                    logger.warning("Search daemon may not be enabled. Some tests may fail.")
            except Exception as e:
                logger.warning(f"Could not check search daemon health: {e}")

            await self.test_search_with_graph_mode_none(client)
            await self.test_search_with_graph_mode_low(client)
            await self.test_search_with_graph_mode_dual(client)
            await self.test_invalid_graph_mode(client)
            await self.test_search_performance(client)

        logger.info("\n" + "=" * 60)
        logger.info(f"Results: {self.passed} passed, {self.failed} failed")
        logger.info("=" * 60)

        return self.failed == 0


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Graph-Enhanced Search API E2E Tests")
    parser.add_argument(
        "--url",
        default=os.environ.get("NEXUS_SERVER_URL", "http://localhost:2026"),
        help="Server URL (default: http://localhost:2026)",
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

    test = GraphSearchAPITest(base_url=args.url, api_key=args.api_key)
    success = await test.run_all_tests()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
