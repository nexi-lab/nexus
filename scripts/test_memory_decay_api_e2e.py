#!/usr/bin/env python3
"""End-to-end test script for Memory Importance Decay API (Issue #1030).

Tests the memory API endpoints with importance decay functionality.

Usage:
    # Start the server first:
    NEXUS_SEARCH_DAEMON=true python -m nexus.cli.main serve

    # Then run the test:
    python scripts/test_memory_decay_api_e2e.py

    # Or specify a different server URL:
    python scripts/test_memory_decay_api_e2e.py --url http://localhost:2027
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class MemoryDecayAPITest:
    """E2E test for memory decay API."""

    def __init__(self, base_url: str, api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("NEXUS_API_KEY", "test-key")
        self.passed = 0
        self.failed = 0
        self.created_memory_ids: list[str] = []

    def _check(self, name: str, condition: bool, message: str = ""):
        """Check a test condition and log result."""
        if condition:
            self.passed += 1
            logger.info(f"  [PASS] {name}")
        else:
            self.failed += 1
            logger.error(f"  [FAIL] {name}: {message}")

    async def test_health_check(self, client: httpx.AsyncClient):
        """Test that the server is healthy."""
        logger.info("\n=== Test: Health Check ===")

        response = await client.get(f"{self.base_url}/health")
        self._check("Health endpoint returns 200", response.status_code == 200)

        if response.status_code == 200:
            data = response.json()
            self._check("Status is healthy", data.get("status") == "healthy")

    async def test_store_memory_with_importance(self, client: httpx.AsyncClient):
        """Test storing a memory with importance."""
        logger.info("\n=== Test: Store Memory with Importance ===")

        memory_content = f"Test memory for decay - {uuid.uuid4()}"
        response = await client.post(
            f"{self.base_url}/api/memory/store",
            json={
                "content": memory_content,
                "scope": "user",
                "memory_type": "fact",
                "importance": 0.8,
            },
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

        self._check("Store returns 200", response.status_code == 200)

        if response.status_code == 200:
            data = response.json()
            self._check("Response has memory_id", "memory_id" in data)
            self._check("Status is created", data.get("status") == "created")

            if "memory_id" in data:
                self.created_memory_ids.append(data["memory_id"])
                logger.info(f"  Created memory: {data['memory_id']}")

    async def test_get_memory_with_decay_fields(self, client: httpx.AsyncClient):
        """Test getting a memory includes decay-related fields."""
        logger.info("\n=== Test: Get Memory with Decay Fields ===")

        if not self.created_memory_ids:
            logger.warning("  No memory to test - skipping")
            return

        memory_id = self.created_memory_ids[0]
        response = await client.get(
            f"{self.base_url}/api/memory/{memory_id}",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

        self._check("Get returns 200", response.status_code == 200)

        if response.status_code == 200:
            data = response.json()
            self._check("Response has memory object", "memory" in data)

            if "memory" in data:
                memory = data["memory"]

                # Check decay-related fields exist
                self._check(
                    "Has importance field",
                    "importance" in memory,
                    f"Fields: {list(memory.keys())}",
                )
                self._check(
                    "Has importance_effective field",
                    "importance_effective" in memory,
                    f"Fields: {list(memory.keys())}",
                )
                self._check(
                    "Has access_count field",
                    "access_count" in memory,
                    f"Fields: {list(memory.keys())}",
                )
                self._check(
                    "Has last_accessed_at field",
                    "last_accessed_at" in memory,
                    f"Fields: {list(memory.keys())}",
                )

                # Check values
                if "importance" in memory and "importance_effective" in memory:
                    logger.info(f"  importance: {memory['importance']}")
                    logger.info(f"  importance_effective: {memory['importance_effective']}")
                    logger.info(f"  access_count: {memory.get('access_count')}")
                    logger.info(f"  last_accessed_at: {memory.get('last_accessed_at')}")

                    # For a just-created memory, effective should equal original (no decay yet)
                    self._check(
                        "Effective importance equals original for new memory",
                        abs(memory["importance_effective"] - (memory.get("importance") or 0.5))
                        < 0.01,
                        f"effective={memory['importance_effective']}, original={memory.get('importance')}",
                    )

    async def test_access_tracking(self, client: httpx.AsyncClient):
        """Test that access count increments on retrieval."""
        logger.info("\n=== Test: Access Tracking ===")

        if not self.created_memory_ids:
            logger.warning("  No memory to test - skipping")
            return

        memory_id = self.created_memory_ids[0]

        # First access (may already be 1 from previous test)
        response1 = await client.get(
            f"{self.base_url}/api/memory/{memory_id}",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        if response1.status_code != 200:
            self._check("First access returns 200", False, f"Got {response1.status_code}")
            return

        count1 = response1.json().get("memory", {}).get("access_count", 0)

        # Second access
        response2 = await client.get(
            f"{self.base_url}/api/memory/{memory_id}",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        if response2.status_code != 200:
            self._check("Second access returns 200", False, f"Got {response2.status_code}")
            return

        count2 = response2.json().get("memory", {}).get("access_count", 0)

        logger.info(f"  Access count before: {count1}")
        logger.info(f"  Access count after: {count2}")

        self._check(
            "Access count incremented",
            count2 > count1,
            f"Expected count to increase from {count1}, got {count2}",
        )

    async def test_track_access_false(self, client: httpx.AsyncClient):
        """Test that track_access=false doesn't increment count."""
        logger.info("\n=== Test: Track Access False ===")

        if not self.created_memory_ids:
            logger.warning("  No memory to test - skipping")
            return

        memory_id = self.created_memory_ids[0]

        # Get current count
        response1 = await client.get(
            f"{self.base_url}/api/memory/{memory_id}?track_access=false",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        if response1.status_code != 200:
            self._check("First read returns 200", False, f"Got {response1.status_code}")
            return

        count1 = response1.json().get("memory", {}).get("access_count", 0)

        # Second access with track_access=false
        response2 = await client.get(
            f"{self.base_url}/api/memory/{memory_id}?track_access=false",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        if response2.status_code != 200:
            self._check("Second read returns 200", False, f"Got {response2.status_code}")
            return

        count2 = response2.json().get("memory", {}).get("access_count", 0)

        logger.info(f"  Access count before: {count1}")
        logger.info(f"  Access count after (track_access=false): {count2}")

        self._check(
            "Access count unchanged with track_access=false",
            count2 == count1,
            f"Expected count to stay {count1}, got {count2}",
        )

    async def test_query_includes_effective_importance(self, client: httpx.AsyncClient):
        """Test that query results include importance_effective."""
        logger.info("\n=== Test: Query Includes Effective Importance ===")

        response = await client.get(
            f"{self.base_url}/api/memory/query",
            params={"scope": "user", "limit": 5},
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

        self._check("Query returns 200", response.status_code == 200)

        if response.status_code == 200:
            data = response.json()
            memories = data.get("memories", [])

            if memories:
                first_memory = memories[0]
                self._check(
                    "Query result has importance_effective",
                    "importance_effective" in first_memory,
                    f"Fields: {list(first_memory.keys())}",
                )
                if "importance_effective" in first_memory:
                    logger.info(
                        f"  First result importance_effective: {first_memory['importance_effective']}"
                    )
            else:
                logger.info("  No memories returned to check")

    async def test_get_nonexistent_memory(self, client: httpx.AsyncClient):
        """Test getting a non-existent memory returns 404."""
        logger.info("\n=== Test: Get Non-existent Memory ===")

        fake_id = f"mem_{uuid.uuid4()}"
        response = await client.get(
            f"{self.base_url}/api/memory/{fake_id}",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

        self._check(
            "Non-existent memory returns 404",
            response.status_code == 404,
            f"Got {response.status_code}",
        )

    async def run_all_tests(self):
        """Run all E2E tests."""
        logger.info("=" * 60)
        logger.info("Memory Importance Decay API E2E Tests (Issue #1030)")
        logger.info(f"Base URL: {self.base_url}")
        logger.info("=" * 60)

        # Disable proxy to connect directly to localhost
        transport = httpx.AsyncHTTPTransport(proxy=None)
        async with httpx.AsyncClient(timeout=30.0, transport=transport) as client:
            await self.test_health_check(client)
            await self.test_store_memory_with_importance(client)
            await self.test_get_memory_with_decay_fields(client)
            await self.test_access_tracking(client)
            await self.test_track_access_false(client)
            await self.test_query_includes_effective_importance(client)
            await self.test_get_nonexistent_memory(client)

        logger.info("\n" + "=" * 60)
        logger.info(f"Results: {self.passed} passed, {self.failed} failed")
        logger.info("=" * 60)

        return self.failed == 0


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Memory Decay API E2E Tests")
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

    test = MemoryDecayAPITest(base_url=args.url, api_key=args.api_key)
    success = await test.run_all_tests()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
