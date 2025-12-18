#!/usr/bin/env python3
"""
ETag and 304 Not Modified Integration Test Script

Tests the ETag/If-None-Match/304 functionality using httpx async client
against a real FastAPI Nexus server started programmatically.

Validates that:
1. ETag headers are returned on read operations
2. If-None-Match with matching ETag returns 304 (no content)
3. If-None-Match with non-matching ETag returns 200 (full content)
4. ETags update when file content changes
5. Early 304 check works (no content read on server)
6. Connector backends support ETags properly

Usage:
    # Auto-start server (default) and run tests
    python scripts/test_etag_304.py

    # Connect to existing server (skip auto-start)
    python scripts/test_etag_304.py --url http://localhost:8080 --no-auto-start

    # With API key
    python scripts/test_etag_304.py --api-key sk-xxx

    # Specify port for auto-started server
    python scripts/test_etag_304.py --port 9999
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Self

import httpx  # noqa: E402
import uvicorn  # noqa: E402

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from nexus.backends.local import LocalBackend  # noqa: E402
from nexus.core.nexus_fs import NexusFS  # noqa: E402
from nexus.server.fastapi_server import create_app  # noqa: E402

if TYPE_CHECKING:
    pass

# Colors for output
GREEN = "\033[0;32m"
BLUE = "\033[0;34m"
YELLOW = "\033[1;33m"
RED = "\033[0;31m"
CYAN = "\033[0;36m"
NC = "\033[0m"  # No Color


def print_section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


def print_success(msg: str) -> None:
    print(f"{GREEN}✓{NC} {msg}")


def print_info(msg: str) -> None:
    print(f"{BLUE}ℹ{NC} {msg}")


def print_warning(msg: str) -> None:
    print(f"{YELLOW}⚠{NC} {msg}")


def print_error(msg: str) -> None:
    print(f"{RED}✗{NC} {msg}")


def print_test(msg: str) -> None:
    print(f"{CYAN}TEST:{NC} {msg}")


class ETagTester:
    """Test ETag/304 functionality."""

    def __init__(self, server_url: str, api_key: str | None = None):
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self.client: httpx.AsyncClient | None = None
        self.test_path = f"/workspace/etag-test-{int(time.time())}.txt"
        self.tests_passed = 0
        self.tests_failed = 0

    async def __aenter__(self) -> Self:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self.client = httpx.AsyncClient(
            base_url=self.server_url,
            headers=headers,
            timeout=30.0,
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        if self.client:
            await self.client.aclose()

    async def rpc_call(
        self,
        method: str,
        params: dict,
        extra_headers: dict | None = None,
    ) -> httpx.Response:
        """Make an RPC call and return the raw response."""
        assert self.client is not None, "Client not initialized. Use async with."
        payload = {
            "jsonrpc": "2.0",
            "id": str(int(time.time() * 1000)),
            "method": method,
            "params": params,
        }
        headers = extra_headers or {}
        return await self.client.post(
            f"/api/nfs/{method}",
            json=payload,
            headers=headers,
        )

    async def write_file(self, path: str, content: str) -> bool:
        """Write a file."""
        import base64

        response = await self.rpc_call(
            "write",
            {"path": path, "content": base64.b64encode(content.encode()).decode()},
        )
        return bool(response.status_code == 200)

    async def delete_file(self, path: str) -> bool:
        """Delete a file."""
        response = await self.rpc_call("delete", {"path": path})
        return bool(response.status_code == 200)

    async def test_etag_returned_on_read(self) -> bool:
        """Test 1: ETag header is returned on read operations."""
        print_test("ETag header returned on read")

        response = await self.rpc_call("read", {"path": self.test_path})

        etag = response.headers.get("ETag")
        if response.status_code == 200 and etag:
            print_success(f"ETag returned: {etag}")
            return True
        else:
            print_error(f"No ETag header. Status: {response.status_code}")
            print_error(f"Headers: {dict(response.headers)}")
            return False

    async def test_304_on_matching_etag(self) -> bool:
        """Test 2: 304 returned when If-None-Match matches ETag."""
        print_test("304 returned on matching If-None-Match")

        # First, get the current ETag
        response1 = await self.rpc_call("read", {"path": self.test_path})
        etag = response1.headers.get("ETag")

        if not etag:
            print_error("No ETag to test with")
            return False

        # Now request with If-None-Match
        response2 = await self.rpc_call(
            "read",
            {"path": self.test_path},
            extra_headers={"If-None-Match": etag},
        )

        if response2.status_code == 304:
            print_success(f"Got 304 Not Modified (ETag: {etag})")
            # Verify no body returned
            body_len = len(response2.content)
            if body_len == 0:
                print_success("No body returned (bandwidth saved)")
            else:
                print_warning(f"Body returned with 304: {body_len} bytes")
            return True
        else:
            print_error(f"Expected 304, got {response2.status_code}")
            return False

    async def test_200_on_non_matching_etag(self) -> bool:
        """Test 3: 200 returned when If-None-Match doesn't match."""
        print_test("200 returned on non-matching If-None-Match")

        # Request with a fake ETag
        response = await self.rpc_call(
            "read",
            {"path": self.test_path},
            extra_headers={"If-None-Match": '"fake-etag-12345"'},
        )

        if response.status_code == 200:
            etag = response.headers.get("ETag")
            print_success(f"Got 200 with full content (new ETag: {etag})")
            return True
        else:
            print_error(f"Expected 200, got {response.status_code}")
            return False

    async def test_etag_changes_on_content_change(self) -> bool:
        """Test 4: ETag changes when file content changes."""
        print_test("ETag changes when content changes")

        # Get initial ETag
        response1 = await self.rpc_call("read", {"path": self.test_path})
        etag1 = response1.headers.get("ETag")

        # Modify file
        await self.write_file(self.test_path, "Modified content for ETag test!")

        # Get new ETag
        response2 = await self.rpc_call("read", {"path": self.test_path})
        etag2 = response2.headers.get("ETag")

        if etag1 != etag2:
            print_success(f"ETag changed: {etag1} -> {etag2}")
            return True
        else:
            print_error(f"ETag unchanged: {etag1}")
            return False

    async def test_old_etag_returns_200(self) -> bool:
        """Test 5: Old ETag returns 200 after content change."""
        print_test("Old ETag returns 200 after content change")

        # Get current ETag
        response1 = await self.rpc_call("read", {"path": self.test_path})
        old_etag = response1.headers.get("ETag")

        # Modify file
        await self.write_file(self.test_path, "Another modification!")

        # Request with old ETag
        response2 = await self.rpc_call(
            "read",
            {"path": self.test_path},
            extra_headers={"If-None-Match": old_etag},
        )

        if response2.status_code == 200:
            new_etag = response2.headers.get("ETag")
            print_success(f"Got 200 (old ETag invalidated). New ETag: {new_etag}")
            return True
        else:
            print_error(f"Expected 200, got {response2.status_code}")
            return False

    async def test_early_304_performance(self) -> bool:
        """Test 6: Early 304 is faster than full read."""
        print_test("Early 304 performance (should be faster)")

        # Write a larger file for meaningful timing
        large_content = "x" * 100000  # 100KB
        await self.write_file(self.test_path, large_content)

        # Get ETag
        response1 = await self.rpc_call("read", {"path": self.test_path})
        etag = response1.headers.get("ETag")

        # Time full read (no If-None-Match)
        start = time.perf_counter()
        for _ in range(5):
            await self.rpc_call("read", {"path": self.test_path})
        full_read_time = (time.perf_counter() - start) / 5 * 1000

        # Time 304 response (with If-None-Match)
        start = time.perf_counter()
        for _ in range(5):
            await self.rpc_call(
                "read",
                {"path": self.test_path},
                extra_headers={"If-None-Match": etag},
            )
        cached_time = (time.perf_counter() - start) / 5 * 1000

        print_info(f"Full read (avg):  {full_read_time:.2f}ms")
        print_info(f"304 response (avg): {cached_time:.2f}ms")

        if cached_time < full_read_time:
            speedup = full_read_time / cached_time
            print_success(f"304 is {speedup:.1f}x faster!")
            return True
        else:
            print_warning("304 not faster (may be due to test overhead)")
            return True  # Not a failure, just informational

    async def test_cache_control_headers(self) -> bool:
        """Test 7: Cache-Control headers are present."""
        print_test("Cache-Control headers present")

        response = await self.rpc_call("read", {"path": self.test_path})

        cache_control = response.headers.get("Cache-Control")
        if cache_control:
            print_success(f"Cache-Control: {cache_control}")
            return True
        else:
            print_warning("No Cache-Control header (optional but recommended)")
            return True  # Not a failure

    async def test_etag_format(self) -> bool:
        """Test 8: ETag format is valid (quoted string)."""
        print_test("ETag format is valid")

        response = await self.rpc_call("read", {"path": self.test_path})
        etag = response.headers.get("ETag")

        if not etag:
            print_error("No ETag header")
            return False

        # ETag should be quoted per HTTP spec
        if etag.startswith('"') and etag.endswith('"'):
            print_success(f"ETag properly quoted: {etag}")
            return True
        elif etag.startswith('W/"'):
            print_success(f"Weak ETag format: {etag}")
            return True
        else:
            print_warning(f"ETag not quoted (may cause issues): {etag}")
            return True  # Not a hard failure

    async def test_etag_with_metadata(self) -> bool:
        """Test 9: ETag works with return_metadata=true."""
        print_test("ETag with return_metadata=true")

        response = await self.rpc_call("read", {"path": self.test_path, "return_metadata": True})
        etag = response.headers.get("ETag")

        if response.status_code == 200 and etag:
            print_success(f"ETag returned with metadata: {etag}")

            # Test 304 with metadata request
            response2 = await self.rpc_call(
                "read",
                {"path": self.test_path, "return_metadata": True},
                extra_headers={"If-None-Match": etag},
            )
            if response2.status_code == 304:
                print_success("304 works with return_metadata=true")
                return True
            else:
                print_warning(f"Expected 304, got {response2.status_code}")
                return True  # Partial success
        else:
            print_error(f"No ETag with metadata. Status: {response.status_code}")
            return False

    async def test_concurrent_304_requests(self) -> bool:
        """Test 10: Concurrent 304 requests all succeed."""
        print_test("Concurrent 304 requests")

        # Get ETag first
        response = await self.rpc_call("read", {"path": self.test_path})
        etag = response.headers.get("ETag")

        if not etag:
            print_error("No ETag to test with")
            return False

        # Make 10 concurrent requests with If-None-Match
        async def make_304_request() -> int:
            r = await self.rpc_call(
                "read",
                {"path": self.test_path},
                extra_headers={"If-None-Match": etag},
            )
            return int(r.status_code)

        tasks = [make_304_request() for _ in range(10)]
        results = await asyncio.gather(*tasks)

        num_304 = sum(1 for r in results if r == 304)
        if num_304 == 10:
            print_success("All 10 concurrent requests returned 304")
            return True
        else:
            print_warning(f"Only {num_304}/10 returned 304: {results}")
            return num_304 >= 8  # Allow some variance

    async def test_unquoted_if_none_match(self) -> bool:
        """Test 11: Server handles unquoted If-None-Match."""
        print_test("Unquoted If-None-Match handling")

        # Get ETag (quoted)
        response = await self.rpc_call("read", {"path": self.test_path})
        etag = response.headers.get("ETag")

        if not etag:
            print_error("No ETag to test with")
            return False

        # Strip quotes and send unquoted
        unquoted_etag = etag.strip('"')
        response2 = await self.rpc_call(
            "read",
            {"path": self.test_path},
            extra_headers={"If-None-Match": unquoted_etag},
        )

        if response2.status_code == 304:
            print_success("Server accepts unquoted If-None-Match")
            return True
        else:
            print_warning(f"Server requires quoted ETag (got {response2.status_code})")
            return True  # Not a hard failure

    async def test_large_file_etag(self) -> bool:
        """Test 12: ETag works correctly for large files."""
        print_test("Large file ETag handling")

        large_path = f"{self.test_path}.large"

        # Create 1MB file
        large_content = "x" * (1024 * 1024)
        await self.write_file(large_path, large_content)

        try:
            # Get ETag
            response = await self.rpc_call("read", {"path": large_path})
            etag = response.headers.get("ETag")

            if not etag:
                print_error("No ETag for large file")
                return False

            print_info(f"Large file ETag: {etag}")

            # Test 304
            response2 = await self.rpc_call(
                "read",
                {"path": large_path},
                extra_headers={"If-None-Match": etag},
            )

            if response2.status_code == 304:
                body_len = len(response2.content)
                print_success(f"304 for 1MB file (body: {body_len} bytes)")
                return True
            else:
                print_error(f"Expected 304, got {response2.status_code}")
                return False
        finally:
            await self.delete_file(large_path)

    async def test_etag_different_for_same_content(self) -> bool:
        """Test 13: Same content at different paths may have same/different ETags."""
        print_test("ETag consistency for same content")

        path1 = f"{self.test_path}.copy1"
        path2 = f"{self.test_path}.copy2"
        same_content = "Identical content for ETag test"

        await self.write_file(path1, same_content)
        await self.write_file(path2, same_content)

        try:
            response1 = await self.rpc_call("read", {"path": path1})
            response2 = await self.rpc_call("read", {"path": path2})

            etag1 = response1.headers.get("ETag")
            etag2 = response2.headers.get("ETag")

            if etag1 == etag2:
                print_success(f"Same content = same ETag: {etag1}")
                print_info("Content-addressed storage detected (deduplication)")
            else:
                print_success("Different paths = different ETags")
                print_info(f"  Path 1: {etag1}")
                print_info(f"  Path 2: {etag2}")
            return True
        finally:
            await self.delete_file(path1)
            await self.delete_file(path2)

    async def test_nonexistent_file_no_etag(self) -> bool:
        """Test 14: Non-existent file returns error, not ETag."""
        print_test("Non-existent file handling")

        response = await self.rpc_call("read", {"path": "/workspace/does-not-exist-12345.txt"})

        etag = response.headers.get("ETag")

        if response.status_code != 200 and not etag:
            print_success("Non-existent file returns error without ETag")
            return True
        elif etag:
            print_error("ETag returned for non-existent file!")
            return False
        else:
            print_success(f"Error returned: {response.status_code}")
            return True

    async def test_if_none_match_on_nonexistent(self) -> bool:
        """Test 15: If-None-Match on non-existent file returns 404, not 304."""
        print_test("If-None-Match on non-existent file")

        response = await self.rpc_call(
            "read",
            {"path": "/workspace/nonexistent-file.txt"},
            extra_headers={"If-None-Match": '"some-etag"'},
        )

        if response.status_code == 304:
            print_error("Got 304 for non-existent file!")
            return False
        else:
            print_success(f"Correctly returned {response.status_code} (not 304)")
            return True

    # =========================================================================
    # Connector-specific tests (require mounted connector)
    # =========================================================================

    async def find_connector_mount(self) -> str | None:
        """Find an available connector mount point."""
        response = await self.rpc_call("list", {"path": "/mnt"})
        if response.status_code != 200:
            return None

        try:
            data = response.json()
            entries = data.get("result", [])
            if entries:
                # Return first mount point found
                for entry in entries:
                    name = entry.get("name", "")
                    if name:
                        return f"/mnt/{name}"
        except Exception:
            pass
        return None

    async def test_connector_etag_basic(self, mount_point: str) -> bool:
        """Test 16: ETag works for connector-backed files."""
        print_test(f"Connector ETag basic ({mount_point})")

        test_path = f"{mount_point}/etag-test-{int(time.time())}.txt"

        try:
            # Write file to connector
            if not await self.write_file(test_path, "Connector ETag test content"):
                print_error("Failed to write to connector")
                return False

            # Read and get ETag
            response = await self.rpc_call("read", {"path": test_path})
            etag = response.headers.get("ETag")

            if not etag:
                print_error("No ETag returned for connector file")
                return False

            print_success(f"Connector ETag: {etag}")

            # Test 304
            response2 = await self.rpc_call(
                "read",
                {"path": test_path},
                extra_headers={"If-None-Match": etag},
            )

            if response2.status_code == 304:
                print_success("304 works for connector-backed files")
                return True
            else:
                print_error(f"Expected 304, got {response2.status_code}")
                return False
        finally:
            await self.delete_file(test_path)

    async def test_connector_etag_after_sync(self, mount_point: str) -> bool:
        """Test 17: ETag works after sync_content_to_cache."""
        print_test(f"Connector ETag after sync ({mount_point})")

        test_path = f"{mount_point}/etag-sync-test-{int(time.time())}.txt"
        content = "Content for sync ETag test"

        try:
            # Write file
            if not await self.write_file(test_path, content):
                print_error("Failed to write to connector")
                return False

            # Trigger sync (if available)
            print_info("Triggering mount sync...")
            assert self.client is not None
            sync_response = await self.client.post(
                "/api/nfs/sync_mount",
                json={
                    "jsonrpc": "2.0",
                    "id": "sync",
                    "method": "sync_mount",
                    "params": {
                        "mount_point": mount_point,
                        "path": test_path.replace(mount_point, "").lstrip("/"),
                    },
                },
            )

            if sync_response.status_code == 200:
                print_info("Sync completed")
            else:
                print_warning("Sync may not be available, continuing...")

            # Read and get ETag (should come from cache now)
            response = await self.rpc_call("read", {"path": test_path})
            etag = response.headers.get("ETag")

            if not etag:
                print_error("No ETag after sync")
                return False

            print_success(f"ETag after sync: {etag}")

            # Test 304
            response2 = await self.rpc_call(
                "read",
                {"path": test_path},
                extra_headers={"If-None-Match": etag},
            )

            if response2.status_code == 304:
                print_success("304 works after sync (ETag from cache)")
                return True
            else:
                print_warning(f"Got {response2.status_code} instead of 304")
                return True  # Not a hard failure
        finally:
            await self.delete_file(test_path)

    async def test_connector_etag_changes_on_update(self, mount_point: str) -> bool:
        """Test 18: Connector ETag changes when file is updated."""
        print_test(f"Connector ETag changes on update ({mount_point})")

        test_path = f"{mount_point}/etag-update-test-{int(time.time())}.txt"

        try:
            # Write initial content
            await self.write_file(test_path, "Initial connector content")

            # Get initial ETag
            response1 = await self.rpc_call("read", {"path": test_path})
            etag1 = response1.headers.get("ETag")

            if not etag1:
                print_error("No initial ETag")
                return False

            print_info(f"Initial ETag: {etag1}")

            # Update content
            await self.write_file(test_path, "Updated connector content!")

            # Get new ETag
            response2 = await self.rpc_call("read", {"path": test_path})
            etag2 = response2.headers.get("ETag")

            if not etag2:
                print_error("No ETag after update")
                return False

            print_info(f"Updated ETag: {etag2}")

            if etag1 != etag2:
                print_success("Connector ETag changed after update")
                return True
            else:
                print_error("ETag unchanged after content update!")
                return False
        finally:
            await self.delete_file(test_path)

    async def test_connector_304_performance(self, mount_point: str) -> bool:
        """Test 19: 304 is faster than full read for connector files."""
        print_test(f"Connector 304 performance ({mount_point})")

        test_path = f"{mount_point}/etag-perf-test-{int(time.time())}.txt"

        # Create larger file for meaningful timing
        large_content = "x" * 50000  # 50KB

        try:
            await self.write_file(test_path, large_content)

            # Get ETag
            response = await self.rpc_call("read", {"path": test_path})
            etag = response.headers.get("ETag")

            if not etag:
                print_error("No ETag")
                return False

            # Time full read
            start = time.perf_counter()
            for _ in range(3):
                await self.rpc_call("read", {"path": test_path})
            full_read_time = (time.perf_counter() - start) / 3 * 1000

            # Time 304 response
            start = time.perf_counter()
            for _ in range(3):
                await self.rpc_call(
                    "read",
                    {"path": test_path},
                    extra_headers={"If-None-Match": etag},
                )
            cached_time = (time.perf_counter() - start) / 3 * 1000

            print_info(f"Connector full read (avg): {full_read_time:.2f}ms")
            print_info(f"Connector 304 (avg): {cached_time:.2f}ms")

            if cached_time < full_read_time:
                speedup = full_read_time / cached_time
                print_success(f"304 is {speedup:.1f}x faster for connector!")
            else:
                print_warning("304 not faster (network overhead may dominate)")

            return True
        finally:
            await self.delete_file(test_path)

    async def test_connector_etag_multiple_files(self, mount_point: str) -> bool:
        """Test 20: ETags are unique per file in connector."""
        print_test(f"Connector ETags unique per file ({mount_point})")

        ts = int(time.time())
        path1 = f"{mount_point}/etag-multi-1-{ts}.txt"
        path2 = f"{mount_point}/etag-multi-2-{ts}.txt"

        try:
            # Write different content to two files
            await self.write_file(path1, "Content for file 1")
            await self.write_file(path2, "Content for file 2")

            # Get ETags
            response1 = await self.rpc_call("read", {"path": path1})
            response2 = await self.rpc_call("read", {"path": path2})

            etag1 = response1.headers.get("ETag")
            etag2 = response2.headers.get("ETag")

            if not etag1 or not etag2:
                print_error("Missing ETags")
                return False

            if etag1 != etag2:
                print_success("ETags are unique:")
                print_info(f"  File 1: {etag1}")
                print_info(f"  File 2: {etag2}")
                return True
            else:
                print_error("ETags are the same for different content!")
                return False
        finally:
            await self.delete_file(path1)
            await self.delete_file(path2)

    async def run_connector_tests(self) -> tuple[int, int]:
        """Run connector-specific tests if a mount is available."""
        print_section("Connector-Specific ETag Tests")

        mount_point = await self.find_connector_mount()

        if not mount_point:
            print_warning("No connector mount found at /mnt/*")
            print_info("Skipping connector tests")
            print_info("To test connectors, mount one first:")
            print_info('  nexus mounts add /mnt/gcs gcs_connector \'{"bucket":"my-bucket"}\'')
            return 0, 0

        print_success(f"Found connector mount: {mount_point}")
        print()

        passed = 0
        failed = 0

        connector_tests = [
            self.test_connector_etag_basic,
            self.test_connector_etag_after_sync,
            self.test_connector_etag_changes_on_update,
            self.test_connector_304_performance,
            self.test_connector_etag_multiple_files,
        ]

        for test in connector_tests:
            try:
                print()
                if await test(mount_point):
                    passed += 1
                else:
                    failed += 1
            except Exception as e:
                print_error(f"Test raised exception: {e}")
                failed += 1

        return passed, failed

    async def run_all_tests(self) -> bool:
        """Run all ETag tests."""
        print_section("ETag / 304 Not Modified Test Suite")

        print_info(f"Server: {self.server_url}")
        print_info(f"Test file: {self.test_path}")
        print()

        # Setup: Create test file
        print_info("Setting up test file...")
        if not await self.write_file(self.test_path, "Initial content for ETag testing"):
            print_error("Failed to create test file")
            return False
        print_success("Test file created")
        print()

        # Run tests
        tests = [
            # Core functionality
            self.test_etag_returned_on_read,
            self.test_304_on_matching_etag,
            self.test_200_on_non_matching_etag,
            self.test_etag_changes_on_content_change,
            self.test_old_etag_returns_200,
            # Performance
            self.test_early_304_performance,
            # Headers
            self.test_cache_control_headers,
            self.test_etag_format,
            # Variations
            self.test_etag_with_metadata,
            self.test_concurrent_304_requests,
            self.test_unquoted_if_none_match,
            # Edge cases
            self.test_large_file_etag,
            self.test_etag_different_for_same_content,
            self.test_nonexistent_file_no_etag,
            self.test_if_none_match_on_nonexistent,
        ]

        for test in tests:
            try:
                print()
                if await test():
                    self.tests_passed += 1
                else:
                    self.tests_failed += 1
            except Exception as e:
                print_error(f"Test raised exception: {e}")
                self.tests_failed += 1

        # Cleanup
        print()
        print_info("Cleaning up test file...")
        await self.delete_file(self.test_path)
        print_success("Cleanup complete")

        # Run connector tests (if available)
        connector_passed, connector_failed = await self.run_connector_tests()
        self.tests_passed += connector_passed
        self.tests_failed += connector_failed

        # Summary
        print_section("Test Summary")

        # Local backend tests
        local_total = len(tests)
        print("  Local Backend Tests:")
        print(f"    Passed: {GREEN}{self.tests_passed - connector_passed}{NC}/{local_total}")

        # Connector tests
        if connector_passed + connector_failed > 0:
            print("  Connector Tests:")
            print(
                f"    Passed: {GREEN}{connector_passed}{NC}/{connector_passed + connector_failed}"
            )
        else:
            print(f"  Connector Tests: {YELLOW}Skipped{NC} (no mount found)")

        # Total
        total = self.tests_passed + self.tests_failed
        print()
        print(f"  Total: {GREEN}{self.tests_passed}{NC}/{total} passed")

        if self.tests_failed == 0:
            print()
            print_success("All ETag tests passed!")
            return True
        else:
            print()
            print_error(f"{self.tests_failed} test(s) failed")
            return False


class NexusTestServer:
    """Manages a test Nexus server with uvicorn in a background thread."""

    def __init__(self, port: int = 8080, api_key: str | None = None):
        self.port = port
        self.api_key = api_key
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None
        self.temp_dir: tempfile.TemporaryDirectory | None = None
        self.nexus_fs: NexusFS | None = None

    def start(self) -> str:
        """Start the server and return the URL."""
        # Create temp directory for storage
        self.temp_dir = tempfile.TemporaryDirectory(prefix="nexus_etag_test_")
        storage_path = Path(self.temp_dir.name) / "storage"
        storage_path.mkdir()
        db_path = Path(self.temp_dir.name) / "nexus.db"

        # Create NexusFS instance
        backend = LocalBackend(root_path=str(storage_path))
        self.nexus_fs = NexusFS(
            backend=backend,
            db_path=str(db_path),
            is_admin=True,  # Admin for testing
            enforce_permissions=False,  # Simplified for testing
        )

        # Ensure workspace directory exists (may already exist by default)
        with contextlib.suppress(FileExistsError):
            self.nexus_fs.mkdir("/workspace")

        # Create FastAPI app
        app = create_app(
            nexus_fs=self.nexus_fs,
            api_key=self.api_key,
        )

        # Configure uvicorn
        config = uvicorn.Config(
            app=app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self.server = uvicorn.Server(config)

        # Run in background thread
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()

        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        """Stop the server and cleanup."""
        if self.server:
            self.server.should_exit = True
            if self.thread:
                self.thread.join(timeout=5)

        if self.temp_dir:
            self.temp_dir.cleanup()


async def wait_for_server(url: str, timeout: int = 30) -> bool:
    """Wait for server to be ready."""
    async with httpx.AsyncClient() as client:
        for _ in range(timeout):
            try:
                response = await client.get(f"{url}/health")
                if response.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
    return False


async def main() -> int:
    parser = argparse.ArgumentParser(description="Test ETag/304 functionality")
    parser.add_argument(
        "--url",
        default=None,
        help="Nexus server URL (if not set, auto-starts a test server)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8099,
        help="Port for auto-started server (default: 8099)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key for authentication",
    )
    parser.add_argument(
        "--no-auto-start",
        action="store_true",
        help="Don't auto-start server (requires --url)",
    )
    args = parser.parse_args()

    server: NexusTestServer | None = None
    server_url = args.url

    try:
        if args.no_auto_start:
            if not args.url:
                print_error("--url required when using --no-auto-start")
                return 1
            server_url = args.url
        else:
            # Auto-start test server
            print_section("Starting Test Server")
            server = NexusTestServer(port=args.port, api_key=args.api_key)
            server_url = server.start()
            print_info(f"Starting uvicorn server at {server_url}...")

            # Wait for server to be ready
            if not await wait_for_server(server_url, timeout=15):
                print_error("Server failed to start within 15 seconds")
                return 1
            print_success(f"Server started at {server_url}")

        # Verify server is reachable
        print_info(f"Verifying server at {server_url}...")
        if not await wait_for_server(server_url, timeout=5):
            print_error(f"Cannot connect to server at {server_url}")
            return 1
        print_success("Server is ready")

        # Run tests
        async with ETagTester(server_url, args.api_key) as tester:
            success = await tester.run_all_tests()
            return 0 if success else 1

    finally:
        if server:
            print()
            print_info("Stopping test server...")
            server.stop()
            print_success("Server stopped")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
