#!/usr/bin/env python3
"""Test bulk delete and rename operations.

This script demonstrates the delete_bulk and rename_bulk operations.
It can run in three modes:
1. Local mode (default): Uses NexusFS directly without a server
2. Server mode: Starts a local server and tests via async client
3. Remote mode: Connects to an existing Nexus server

Usage:
    # Local mode (no server needed):
    python examples/cli/test_bulk_operations.py

    # Server mode (starts local server):
    python examples/cli/test_bulk_operations.py --server
    python examples/cli/test_bulk_operations.py --server --port 8091

    # Remote mode (connect to existing server):
    NEXUS_URL="http://localhost:8080" python examples/cli/test_bulk_operations.py --remote
"""

import argparse
import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# Add src to path for local development
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

# Colors for output
GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[1;33m"
BLUE = "\033[0;34m"
CYAN = "\033[0;36m"
NC = "\033[0m"


def print_header(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def print_success(msg: str) -> None:
    print(f"{GREEN}✓{NC} {msg}")


def print_error(msg: str) -> None:
    print(f"{RED}✗{NC} {msg}")


def print_info(msg: str) -> None:
    print(f"{BLUE}ℹ{NC} {msg}")


def print_test(msg: str) -> None:
    print(f"{CYAN}TEST:{NC} {msg}")


class ServerProcess:
    """Manages a local Nexus server process."""

    def __init__(self, port: int = 8090):
        self.port = port
        self.process: subprocess.Popen | None = None
        self.url = f"http://localhost:{port}"
        self.db_file = f"/tmp/nexus-bulk-test-{port}.db"

    def start(self) -> bool:
        """Start the server and wait for it to be ready."""
        print_info(f"Starting Nexus server on port {self.port}...")

        # Clean up old database
        if os.path.exists(self.db_file):
            os.remove(self.db_file)

        env = os.environ.copy()
        env["NEXUS_DATABASE_URL"] = f"sqlite:///{self.db_file}"

        self.process = subprocess.Popen(
            ["uv", "run", "nexus", "serve", "--port", str(self.port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            preexec_fn=os.setsid,
        )

        # Wait for server to be ready
        for _ in range(30):
            try:
                urllib.request.urlopen(f"{self.url}/health", timeout=1)
                print_success(f"Server ready at {self.url}")
                return True
            except (urllib.error.URLError, TimeoutError):
                time.sleep(1)

        print_error("Server failed to start within 30 seconds")
        self.stop()
        return False

    def stop(self) -> None:
        """Stop the server."""
        if self.process:
            print_info("Stopping server...")
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                self.process.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
            self.process = None
            print_success("Server stopped")

        # Clean up database
        if os.path.exists(self.db_file):
            os.remove(self.db_file)


class TestRunner:
    """Base class for test runners."""

    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.base = "/workspace/bulk-ops-test"

    def record(self, name: str, success: bool) -> None:
        if success:
            self.passed += 1
            print_success(name)
        else:
            self.failed += 1
            print_error(name)

    def summary(self) -> bool:
        print_header("Test Summary")
        total = self.passed + self.failed
        if self.failed == 0:
            print_success(f"All {total} tests passed!")
            return True
        else:
            print_error(f"{self.failed}/{total} tests failed")
            return False


class LocalTestRunner(TestRunner):
    """Run tests using local NexusFS (no server needed)."""

    def __init__(self, data_dir: str):
        super().__init__()
        self.data_dir = data_dir

    def run(self) -> bool:
        from nexus.backends import LocalBackend
        from nexus.core.nexus_fs import NexusFS

        print_header("Bulk Operations Test (Local Mode)")
        print_info(f"Data directory: {self.data_dir}")

        # Use a database in the temp directory to ensure isolation between test runs
        db_path = Path(self.data_dir) / "nexus-test.db"
        backend = LocalBackend(self.data_dir)
        # Disable permission enforcement for local testing to avoid permission errors
        nx = NexusFS(backend=backend, db_path=db_path, enforce_permissions=False)

        try:
            self._run_tests(nx)
            return self.summary()
        finally:
            nx.close()

    def _run_tests(self, nx) -> None:
        # Setup
        print_header("Setup: Create Test Files")
        nx.mkdir(self.base, parents=True, exist_ok=True)
        print_success(f"Created directory: {self.base}")

        test_files = [
            f"{self.base}/file1.txt",
            f"{self.base}/file2.txt",
            f"{self.base}/file3.txt",
            f"{self.base}/subdir/nested1.txt",
            f"{self.base}/subdir/nested2.txt",
        ]

        for f in test_files:
            parent = str(Path(f).parent)
            if parent != self.base:
                nx.mkdir(parent, parents=True, exist_ok=True)
            nx.write(f, f"Content of {f}".encode())
            print_success(f"Created: {f}")

        # Test 1: rename_bulk
        print_header("Test 1: rename_bulk")
        print_test("Renaming multiple files in a single call")

        renames = [
            (f"{self.base}/file1.txt", f"{self.base}/renamed1.txt"),
            (f"{self.base}/file2.txt", f"{self.base}/renamed2.txt"),
            (f"{self.base}/subdir/nested1.txt", f"{self.base}/subdir/moved1.txt"),
        ]

        result = nx.rename_bulk(renames)
        print_info(f"Result: {result}")

        all_success = all(
            result.get(old, {}).get("success", False) and nx.exists(new) for old, new in renames
        )
        for old, new in renames:
            if result.get(old, {}).get("success"):
                print_success(f"Renamed: {old} -> {new}")
            else:
                print_error(f"Failed: {old}")

        self.record("rename_bulk: All renames successful", all_success)

        # Test 2: rename_bulk with errors
        print_header("Test 2: rename_bulk with non-existent files")
        print_test("Attempting to rename files that don't exist")

        bad_renames = [
            (f"{self.base}/nonexistent1.txt", f"{self.base}/target1.txt"),
            (f"{self.base}/renamed1.txt", f"{self.base}/good_rename.txt"),
        ]

        result = nx.rename_bulk(bad_renames)
        print_info(f"Result: {result}")

        first_failed = not result.get(f"{self.base}/nonexistent1.txt", {}).get("success", True)
        second_success = result.get(f"{self.base}/renamed1.txt", {}).get("success", False)

        self.record(
            "rename_bulk correctly handles partial failures", first_failed and second_success
        )

        # Test 3: delete_bulk
        print_header("Test 3: delete_bulk")
        print_test("Deleting multiple files in a single call")

        files_to_delete = [
            f"{self.base}/good_rename.txt",
            f"{self.base}/renamed2.txt",
            f"{self.base}/file3.txt",
        ]

        result = nx.delete_bulk(files_to_delete)
        print_info(f"Result: {result}")

        all_deleted = all(
            result.get(p, {}).get("success", False) and not nx.exists(p) for p in files_to_delete
        )
        for p in files_to_delete:
            if result.get(p, {}).get("success"):
                print_success(f"Deleted: {p}")
            else:
                print_error(f"Failed: {p}")

        self.record("delete_bulk: All deletes successful", all_deleted)

        # Test 4: delete_bulk with mixed results
        print_header("Test 4: delete_bulk with mixed results")
        print_test("Deleting mix of existing and non-existing files")

        mixed_delete = [
            f"{self.base}/subdir/moved1.txt",
            f"{self.base}/subdir/nested2.txt",
            f"{self.base}/nonexistent.txt",
        ]

        result = nx.delete_bulk(mixed_delete)
        print_info(f"Result: {result}")

        first_ok = result.get(f"{self.base}/subdir/moved1.txt", {}).get("success", False)
        second_ok = result.get(f"{self.base}/subdir/nested2.txt", {}).get("success", False)
        third_fail = not result.get(f"{self.base}/nonexistent.txt", {}).get("success", True)

        self.record(
            "delete_bulk correctly handles mixed results", first_ok and second_ok and third_fail
        )

        # Test 5: delete_bulk with recursive (explicit directory)
        print_header("Test 5: delete_bulk with recursive directory deletion (explicit)")
        print_test("Deleting EXPLICIT directory with recursive=True")

        nx.mkdir(f"{self.base}/to_delete", exist_ok=True)
        nx.write(f"{self.base}/to_delete/a.txt", b"file a")
        nx.write(f"{self.base}/to_delete/b.txt", b"file b")
        nx.mkdir(f"{self.base}/to_delete/sub", exist_ok=True)
        nx.write(f"{self.base}/to_delete/sub/c.txt", b"file c")

        result = nx.delete_bulk([f"{self.base}/to_delete"], recursive=True)
        print_info(f"Result: {result}")

        success = result.get(f"{self.base}/to_delete", {}).get("success", False) and not nx.exists(
            f"{self.base}/to_delete"
        )
        self.record("delete_bulk with recursive=True works for explicit directory", success)

        # Test 6: delete_bulk on IMPLICIT directory
        print_header("Test 6: delete_bulk on IMPLICIT directory")
        print_test("Deleting IMPLICIT directory (no mkdir, just files under it)")

        # Create files directly without mkdir - this creates an implicit directory
        nx.write(f"{self.base}/implicit_dir/file1.txt", b"file 1")
        nx.write(f"{self.base}/implicit_dir/file2.txt", b"file 2")
        nx.write(f"{self.base}/implicit_dir/nested/file3.txt", b"file 3")

        # Verify the implicit directory exists
        implicit_exists = nx.exists(f"{self.base}/implicit_dir")
        print_info(f"Implicit directory exists: {implicit_exists}")

        result = nx.delete_bulk([f"{self.base}/implicit_dir"], recursive=True)
        print_info(f"Result: {result}")

        success = result.get(f"{self.base}/implicit_dir", {}).get(
            "success", False
        ) and not nx.exists(f"{self.base}/implicit_dir")
        self.record("delete_bulk works for IMPLICIT directory", success)

        # Test 7: rename_bulk on IMPLICIT directory
        print_header("Test 7: rename_bulk on IMPLICIT directory")
        print_test("Renaming IMPLICIT directory (no mkdir, just files under it)")

        # Create another implicit directory
        nx.write(f"{self.base}/implicit_rename/a.txt", b"file a")
        nx.write(f"{self.base}/implicit_rename/b.txt", b"file b")

        # Verify it exists
        implicit_exists = nx.exists(f"{self.base}/implicit_rename")
        print_info(f"Implicit directory exists before rename: {implicit_exists}")

        result = nx.rename_bulk([(f"{self.base}/implicit_rename", f"{self.base}/implicit_renamed")])
        print_info(f"Result: {result}")

        # Check the rename worked
        old_gone = not nx.exists(f"{self.base}/implicit_rename")
        new_exists = nx.exists(f"{self.base}/implicit_renamed")
        files_moved = nx.exists(f"{self.base}/implicit_renamed/a.txt") and nx.exists(
            f"{self.base}/implicit_renamed/b.txt"
        )

        success = (
            result.get(f"{self.base}/implicit_rename", {}).get("success", False)
            and old_gone
            and new_exists
            and files_moved
        )
        self.record("rename_bulk works for IMPLICIT directory", success)

        # Cleanup
        print_header("Cleanup")
        nx.delete_bulk(
            [self.base, f"{self.base}/subdir", f"{self.base}/implicit_renamed"], recursive=True
        )
        print_success("Test directory cleaned up")


class RemoteTestRunner(TestRunner):
    """Run tests using async remote client."""

    def __init__(self, url: str, api_key: str | None = None):
        super().__init__()
        self.url = url
        self.api_key = api_key or "test"

    def run(self) -> bool:
        return asyncio.run(self._run_async())

    async def _run_async(self) -> bool:
        from nexus.remote import AsyncRemoteNexusFS

        print_header("Bulk Operations Test (Remote Mode)")
        print_info(f"Server: {self.url}")

        nx = AsyncRemoteNexusFS(self.url, api_key=self.api_key)

        try:
            # Cleanup stale data
            print_header("Cleanup: Remove Stale Test Data")
            try:
                await nx.delete_bulk([self.base], recursive=True)
                print_success("Cleaned up previous test data")
            except Exception:
                print_info("No previous test data to clean up")

            await self._run_tests(nx)
            return self.summary()
        finally:
            await nx.close()

    async def _run_tests(self, nx) -> None:
        # Setup
        print_header("Setup: Create Test Files")
        await nx.mkdir(self.base, parents=True, exist_ok=True)
        print_success(f"Created directory: {self.base}")

        test_files = [
            f"{self.base}/file1.txt",
            f"{self.base}/file2.txt",
            f"{self.base}/file3.txt",
            f"{self.base}/subdir/nested1.txt",
            f"{self.base}/subdir/nested2.txt",
        ]

        for f in test_files:
            parent = str(Path(f).parent)
            if parent != self.base:
                await nx.mkdir(parent, parents=True, exist_ok=True)
            await nx.write(f, f"Content of {f}".encode())
            print_success(f"Created: {f}")

        # Test 1: rename_bulk
        print_header("Test 1: rename_bulk")
        print_test("Renaming multiple files in a single call")

        renames = [
            (f"{self.base}/file1.txt", f"{self.base}/renamed1.txt"),
            (f"{self.base}/file2.txt", f"{self.base}/renamed2.txt"),
            (f"{self.base}/subdir/nested1.txt", f"{self.base}/subdir/moved1.txt"),
        ]

        result = await nx.rename_bulk(renames)
        print_info(f"Result: {result}")

        all_success = True
        for old, new in renames:
            if result.get(old, {}).get("success") and await nx.exists(new):
                print_success(f"Renamed: {old} -> {new}")
            else:
                print_error(f"Failed: {old}")
                all_success = False

        self.record("rename_bulk: All renames successful", all_success)

        # Test 2: rename_bulk with errors
        print_header("Test 2: rename_bulk with non-existent files")
        print_test("Attempting to rename files that don't exist")

        bad_renames = [
            (f"{self.base}/nonexistent1.txt", f"{self.base}/target1.txt"),
            (f"{self.base}/renamed1.txt", f"{self.base}/good_rename.txt"),
        ]

        result = await nx.rename_bulk(bad_renames)
        print_info(f"Result: {result}")

        first_failed = not result.get(f"{self.base}/nonexistent1.txt", {}).get("success", True)
        second_success = result.get(f"{self.base}/renamed1.txt", {}).get("success", False)

        self.record(
            "rename_bulk correctly handles partial failures", first_failed and second_success
        )

        # Test 3: delete_bulk
        print_header("Test 3: delete_bulk")
        print_test("Deleting multiple files in a single call")

        files_to_delete = [
            f"{self.base}/good_rename.txt",
            f"{self.base}/renamed2.txt",
            f"{self.base}/file3.txt",
        ]

        result = await nx.delete_bulk(files_to_delete)
        print_info(f"Result: {result}")

        all_deleted = True
        for p in files_to_delete:
            if result.get(p, {}).get("success") and not await nx.exists(p):
                print_success(f"Deleted: {p}")
            else:
                print_error(f"Failed: {p}")
                all_deleted = False

        self.record("delete_bulk: All deletes successful", all_deleted)

        # Test 4: delete_bulk with mixed results
        print_header("Test 4: delete_bulk with mixed results")
        print_test("Deleting mix of existing and non-existing files")

        mixed_delete = [
            f"{self.base}/subdir/moved1.txt",
            f"{self.base}/subdir/nested2.txt",
            f"{self.base}/nonexistent.txt",
        ]

        result = await nx.delete_bulk(mixed_delete)
        print_info(f"Result: {result}")

        first_ok = result.get(f"{self.base}/subdir/moved1.txt", {}).get("success", False)
        second_ok = result.get(f"{self.base}/subdir/nested2.txt", {}).get("success", False)
        third_fail = not result.get(f"{self.base}/nonexistent.txt", {}).get("success", True)

        self.record(
            "delete_bulk correctly handles mixed results", first_ok and second_ok and third_fail
        )

        # Test 5: delete_bulk with recursive (explicit directory)
        print_header("Test 5: delete_bulk with recursive directory deletion (explicit)")
        print_test("Deleting EXPLICIT directory with recursive=True")

        await nx.mkdir(f"{self.base}/to_delete", exist_ok=True)
        await nx.write(f"{self.base}/to_delete/a.txt", b"file a")
        await nx.write(f"{self.base}/to_delete/b.txt", b"file b")
        await nx.mkdir(f"{self.base}/to_delete/sub", exist_ok=True)
        await nx.write(f"{self.base}/to_delete/sub/c.txt", b"file c")

        result = await nx.delete_bulk([f"{self.base}/to_delete"], recursive=True)
        print_info(f"Result: {result}")

        success = result.get(f"{self.base}/to_delete", {}).get(
            "success", False
        ) and not await nx.exists(f"{self.base}/to_delete")
        self.record("delete_bulk with recursive=True works for explicit directory", success)

        # Test 6: delete_bulk on IMPLICIT directory
        print_header("Test 6: delete_bulk on IMPLICIT directory")
        print_test("Deleting IMPLICIT directory (no mkdir, just files under it)")

        # Create files directly without mkdir - this creates an implicit directory
        await nx.write(f"{self.base}/implicit_dir/file1.txt", b"file 1")
        await nx.write(f"{self.base}/implicit_dir/file2.txt", b"file 2")
        await nx.write(f"{self.base}/implicit_dir/nested/file3.txt", b"file 3")

        # Verify the implicit directory exists
        implicit_exists = await nx.exists(f"{self.base}/implicit_dir")
        print_info(f"Implicit directory exists: {implicit_exists}")

        result = await nx.delete_bulk([f"{self.base}/implicit_dir"], recursive=True)
        print_info(f"Result: {result}")

        success = result.get(f"{self.base}/implicit_dir", {}).get(
            "success", False
        ) and not await nx.exists(f"{self.base}/implicit_dir")
        self.record("delete_bulk works for IMPLICIT directory", success)

        # Test 7: rename_bulk on IMPLICIT directory
        print_header("Test 7: rename_bulk on IMPLICIT directory")
        print_test("Renaming IMPLICIT directory (no mkdir, just files under it)")

        # Create another implicit directory
        await nx.write(f"{self.base}/implicit_rename/a.txt", b"file a")
        await nx.write(f"{self.base}/implicit_rename/b.txt", b"file b")

        # Verify it exists
        implicit_exists = await nx.exists(f"{self.base}/implicit_rename")
        print_info(f"Implicit directory exists before rename: {implicit_exists}")

        result = await nx.rename_bulk(
            [(f"{self.base}/implicit_rename", f"{self.base}/implicit_renamed")]
        )
        print_info(f"Result: {result}")

        # Check the rename worked
        old_gone = not await nx.exists(f"{self.base}/implicit_rename")
        new_exists = await nx.exists(f"{self.base}/implicit_renamed")
        files_moved = await nx.exists(f"{self.base}/implicit_renamed/a.txt") and await nx.exists(
            f"{self.base}/implicit_renamed/b.txt"
        )

        success = (
            result.get(f"{self.base}/implicit_rename", {}).get("success", False)
            and old_gone
            and new_exists
            and files_moved
        )
        self.record("rename_bulk works for IMPLICIT directory", success)

        # Cleanup
        print_header("Cleanup")
        await nx.delete_bulk(
            [self.base, f"{self.base}/subdir", f"{self.base}/implicit_renamed"], recursive=True
        )
        print_success("Test directory cleaned up")


def main() -> int:
    parser = argparse.ArgumentParser(description="Test bulk delete and rename operations")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--server",
        action="store_true",
        help="Start a local server and test via async client",
    )
    mode_group.add_argument(
        "--remote",
        action="store_true",
        help="Connect to an existing server",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8090,
        help="Port for local server (default: 8090)",
    )
    parser.add_argument(
        "--url",
        type=str,
        default=os.environ.get("NEXUS_URL", "http://localhost:8080"),
        help="Nexus server URL for remote mode (default: $NEXUS_URL or http://localhost:8080)",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=os.environ.get("NEXUS_API_KEY"),
        help="API key for server/remote mode (default: $NEXUS_API_KEY)",
    )

    args = parser.parse_args()

    if args.server:
        # Server mode - start local server and test
        server = ServerProcess(port=args.port)
        try:
            if not server.start():
                return 1
            runner = RemoteTestRunner(server.url, args.api_key)
            success = runner.run()
        finally:
            server.stop()
    elif args.remote:
        # Remote mode - connect to existing server
        runner = RemoteTestRunner(args.url, args.api_key)
        success = runner.run()
    else:
        # Local mode - use NexusFS directly
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = LocalTestRunner(tmpdir)
            success = runner.run()

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
