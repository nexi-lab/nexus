"""Test script to reproduce and confirm thread pool exhaustion issue.

This test simulates the scenario where:
1. Cache expires (TTL = 300s)
2. Multiple concurrent requests hit cold cache
3. Thread pool gets exhausted
4. New requests hang indefinitely

Run with:
    python tests/benchmarks/test_thread_pool_exhaustion.py --url http://localhost:8080

Or run the in-process test:
    python tests/benchmarks/test_thread_pool_exhaustion.py --in-process
"""

import argparse
import asyncio
import os
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from nexus.factory import create_nexus_fs
from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


@dataclass
class RequestResult:
    """Result of a single request."""

    request_id: int
    start_time: float
    end_time: float
    duration: float
    success: bool
    error: str | None = None
    thread_name: str = ""


@dataclass
class TestResults:
    """Aggregated test results."""

    total_requests: int = 0
    successful: int = 0
    failed: int = 0
    timed_out: int = 0
    durations: list[float] = field(default_factory=list)
    results: list[RequestResult] = field(default_factory=list)

    def add(self, result: RequestResult) -> None:
        self.total_requests += 1
        self.results.append(result)
        if result.success:
            self.successful += 1
            self.durations.append(result.duration)
        elif result.error and "timeout" in result.error.lower():
            self.timed_out += 1
        else:
            self.failed += 1

    def summary(self) -> str:
        lines = [
            "\n" + "=" * 60,
            "TEST RESULTS SUMMARY",
            "=" * 60,
            f"Total requests:  {self.total_requests}",
            f"Successful:      {self.successful}",
            f"Failed:          {self.failed}",
            f"Timed out:       {self.timed_out}",
        ]

        if self.durations:
            lines.extend(
                [
                    "",
                    "Response Times (successful requests):",
                    f"  Min:    {min(self.durations):.3f}s",
                    f"  Max:    {max(self.durations):.3f}s",
                    f"  Mean:   {statistics.mean(self.durations):.3f}s",
                    f"  Median: {statistics.median(self.durations):.3f}s",
                ]
            )
            if len(self.durations) > 1:
                lines.append(f"  StdDev: {statistics.stdev(self.durations):.3f}s")

        # Show timeline
        lines.extend(["", "Request Timeline:"])
        sorted_results = sorted(self.results, key=lambda r: r.start_time)
        base_time = sorted_results[0].start_time if sorted_results else 0

        for r in sorted_results[:20]:  # Show first 20
            status = "OK" if r.success else f"FAIL: {r.error}"
            lines.append(
                f"  [{r.request_id:3d}] T+{r.start_time - base_time:6.2f}s "
                f"-> {r.duration:6.2f}s [{r.thread_name}] {status}"
            )

        if len(sorted_results) > 20:
            lines.append(f"  ... and {len(sorted_results) - 20} more")

        lines.append("=" * 60)
        return "\n".join(lines)


# =============================================================================
# HTTP Client Test (against running server)
# =============================================================================


def test_http_concurrent_requests(
    base_url: str,
    num_requests: int = 20,
    timeout: float = 60.0,
    path: str = "/",
) -> TestResults:
    """Test thread pool exhaustion via HTTP requests."""
    import requests

    results = TestResults()

    def make_request(request_id: int) -> RequestResult:
        thread_name = threading.current_thread().name
        start = time.time()
        try:
            # Use JSON-RPC format
            response = requests.post(
                f"{base_url}/rpc",
                json={
                    "jsonrpc": "2.0",
                    "method": "list",
                    "params": {"path": path, "recursive": False},
                    "id": request_id,
                },
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )
            end = time.time()

            if response.status_code == 200:
                return RequestResult(
                    request_id=request_id,
                    start_time=start,
                    end_time=end,
                    duration=end - start,
                    success=True,
                    thread_name=thread_name,
                )
            else:
                return RequestResult(
                    request_id=request_id,
                    start_time=start,
                    end_time=end,
                    duration=end - start,
                    success=False,
                    error=f"HTTP {response.status_code}",
                    thread_name=thread_name,
                )
        except requests.Timeout:
            end = time.time()
            return RequestResult(
                request_id=request_id,
                start_time=start,
                end_time=end,
                duration=end - start,
                success=False,
                error="Request timeout",
                thread_name=thread_name,
            )
        except Exception as e:
            end = time.time()
            return RequestResult(
                request_id=request_id,
                start_time=start,
                end_time=end,
                duration=end - start,
                success=False,
                error=str(e),
                thread_name=thread_name,
            )

    print(f"\nSending {num_requests} concurrent requests to {base_url}...")
    print(f"Path: {path}, Timeout: {timeout}s")
    print("-" * 60)

    # Use more threads than server's pool to trigger exhaustion
    with ThreadPoolExecutor(max_workers=num_requests) as executor:
        futures = {executor.submit(make_request, i): i for i in range(num_requests)}

        for future in as_completed(futures):
            result = future.result()
            results.add(result)
            status = "OK" if result.success else f"FAIL ({result.error})"
            print(f"  Request {result.request_id:3d}: {result.duration:.2f}s - {status}")

    return results


# =============================================================================
# In-Process Test (direct NexusFS calls)
# =============================================================================


def test_in_process_thread_exhaustion(
    num_requests: int = 20,
    timeout: float = 60.0,
) -> TestResults:
    """Test thread pool exhaustion with in-process NexusFS."""
    from nexus.backends.local import LocalBackend
    from nexus.core.permissions import OperationContext

    results = TestResults()

    # Create temporary directory with test files
    with tempfile.TemporaryDirectory() as tmpdir:
        print(f"\nSetting up test environment in {tmpdir}...")

        # Initialize NexusFS
        db_path = os.path.join(tmpdir, "nexus.db")
        backend = LocalBackend(root_path=tmpdir)

        # Create NexusFS without permissions for setup
        nx = create_nexus_fs(
            backend=backend,
            metadata_store=RaftMetadataStore.embedded(db_path.replace(".db", "-raft")),
            record_store=SQLAlchemyRecordStore(db_path=db_path),
            enforce_permissions=False,
        )

        # Create test files (no permission check needed)
        for i in range(50):
            path = f"/test_file_{i}.txt"
            nx.write(path, f"Test content {i}".encode())

        # Now enable permissions
        nx._enforce_permissions = True

        # Create a test user context
        context = OperationContext(
            user="test_user",
            groups=[],
            zone_id="default",
            subject_type="user",
            subject_id="test_user",
        )

        # Grant read permission to test user
        nx.rebac_create(
            subject=("user", "test_user"),
            relation="reader",
            object=("file", "/"),
            zone_id="default",
        )

        print("Created 50 test files")
        print(f"Database: {db_path}")

        # Clear caches to simulate cold start
        print("\nClearing caches to simulate cold start...")
        if hasattr(nx, "_rebac_manager"):
            if hasattr(nx._rebac_manager, "_zone_graph_cache"):
                nx._rebac_manager._zone_graph_cache.clear()
            if hasattr(nx._rebac_manager, "_l1_cache") and nx._rebac_manager._l1_cache:
                nx._rebac_manager._l1_cache.clear()

        def make_list_call(request_id: int) -> RequestResult:
            thread_name = threading.current_thread().name
            start = time.time()
            try:
                # This is the slow path - list with permission checks
                _result = nx.list("/", recursive=False, context=context)
                end = time.time()
                return RequestResult(
                    request_id=request_id,
                    start_time=start,
                    end_time=end,
                    duration=end - start,
                    success=True,
                    thread_name=thread_name,
                )
            except Exception as e:
                end = time.time()
                return RequestResult(
                    request_id=request_id,
                    start_time=start,
                    end_time=end,
                    duration=end - start,
                    success=False,
                    error=str(e),
                    thread_name=thread_name,
                )

        print(f"\nSending {num_requests} concurrent list() calls...")
        print("-" * 60)

        # Simulate asyncio.to_thread behavior with ThreadPoolExecutor
        # Using default pool size to match server behavior
        default_pool_size = min(32, (os.cpu_count() or 1) + 4)
        print(f"Thread pool size: {default_pool_size} (Python default)")

        with ThreadPoolExecutor(max_workers=default_pool_size) as executor:
            # Submit more requests than pool size
            futures = {executor.submit(make_list_call, i): i for i in range(num_requests)}

            for future in as_completed(futures, timeout=timeout):
                try:
                    result = future.result(timeout=1.0)
                    results.add(result)
                    status = "OK" if result.success else f"FAIL ({result.error})"
                    print(f"  Request {result.request_id:3d}: {result.duration:.2f}s - {status}")
                except Exception as e:
                    print(f"  Request failed: {e}")

    return results


# =============================================================================
# Async simulation (matching FastAPI server behavior)
# =============================================================================


async def test_async_thread_exhaustion(
    num_requests: int = 20,
    timeout: float = 60.0,
) -> TestResults:
    """Test that simulates exact FastAPI server behavior with asyncio.to_thread."""
    from nexus.backends.local import LocalBackend
    from nexus.core.permissions import OperationContext

    results = TestResults()

    with tempfile.TemporaryDirectory() as tmpdir:
        print(f"\nSetting up async test environment in {tmpdir}...")

        db_path = os.path.join(tmpdir, "nexus.db")
        backend = LocalBackend(root_path=tmpdir)

        # Create NexusFS without permissions for setup
        nx = create_nexus_fs(
            backend=backend,
            metadata_store=RaftMetadataStore.embedded(db_path.replace(".db", "-raft")),
            record_store=SQLAlchemyRecordStore(db_path=db_path),
            enforce_permissions=False,  # Disable for setup
        )

        # Create test files (no permission check needed)
        for i in range(100):
            path = f"/test_file_{i}.txt"
            nx.write(path, f"Test content {i}".encode())

        # Now enable permissions
        nx._enforce_permissions = True

        # Create test user context
        context = OperationContext(
            user="test_user",
            groups=[],
            zone_id="default",
            subject_type="user",
            subject_id="test_user",
        )

        # Grant test user read permission on root
        nx.rebac_create(
            subject=("user", "test_user"),
            relation="reader",
            object=("file", "/"),
            zone_id="default",
        )

        print("Created 100 test files")

        # FORCE WORST CASE: Disable Rust acceleration to simulate slow Python path
        import nexus.services.permissions.rebac_fast as rebac_fast

        _original_rust_available = rebac_fast.RUST_AVAILABLE  # noqa: F841
        rebac_fast.RUST_AVAILABLE = False
        print("*** DISABLED RUST ACCELERATION (simulating worst case) ***")

        # Clear caches
        if hasattr(nx, "_rebac_manager"):
            if hasattr(nx._rebac_manager, "_zone_graph_cache"):
                nx._rebac_manager._zone_graph_cache.clear()
            if hasattr(nx._rebac_manager, "_l1_cache") and nx._rebac_manager._l1_cache:
                nx._rebac_manager._l1_cache.clear()

        def sync_list_operation(request_id: int) -> RequestResult:
            """Sync operation that will be run in thread pool."""
            thread_name = threading.current_thread().name
            start = time.time()
            try:
                _result = nx.list("/", recursive=False, context=context)
                end = time.time()
                return RequestResult(
                    request_id=request_id,
                    start_time=start,
                    end_time=end,
                    duration=end - start,
                    success=True,
                    thread_name=thread_name,
                )
            except Exception as e:
                end = time.time()
                return RequestResult(
                    request_id=request_id,
                    start_time=start,
                    end_time=end,
                    duration=end - start,
                    success=False,
                    error=str(e),
                    thread_name=thread_name,
                )

        async def async_list_wrapper(request_id: int) -> RequestResult:
            """Async wrapper using asyncio.to_thread (matches FastAPI behavior)."""
            return await asyncio.to_thread(sync_list_operation, request_id)

        print(f"\nSending {num_requests} concurrent async requests...")
        print("Using asyncio.to_thread (matches FastAPI server behavior)")
        print("-" * 60)

        # Get current thread pool info
        loop = asyncio.get_event_loop()
        executor = loop._default_executor
        if executor:
            print(f"Current executor: {type(executor).__name__}")
            if hasattr(executor, "_max_workers"):
                print(f"Max workers: {executor._max_workers}")

        # Launch all requests concurrently
        tasks = [async_list_wrapper(i) for i in range(num_requests)]

        try:
            completed = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=timeout,
            )

            for i, result in enumerate(completed):
                if isinstance(result, Exception):
                    results.add(
                        RequestResult(
                            request_id=i,
                            start_time=0,
                            end_time=0,
                            duration=0,
                            success=False,
                            error=str(result),
                        )
                    )
                else:
                    results.add(result)
                    status = "OK" if result.success else f"FAIL ({result.error})"
                    print(f"  Request {result.request_id:3d}: {result.duration:.2f}s - {status}")

        except TimeoutError:
            print(f"\n*** TIMEOUT: Not all requests completed within {timeout}s ***")
            print("This indicates thread pool exhaustion!")

    return results


# =============================================================================
# Main
# =============================================================================


def main():
    parser = argparse.ArgumentParser(description="Test thread pool exhaustion in Nexus server")
    parser.add_argument(
        "--url",
        default=None,
        help="Nexus server URL (e.g., http://localhost:8080)",
    )
    parser.add_argument(
        "--in-process",
        action="store_true",
        help="Run in-process test (no server needed)",
    )
    parser.add_argument(
        "--async",
        dest="use_async",
        action="store_true",
        help="Run async test (simulates FastAPI behavior)",
    )
    parser.add_argument(
        "--requests",
        type=int,
        default=20,
        help="Number of concurrent requests (default: 20)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Request timeout in seconds (default: 60)",
    )
    parser.add_argument(
        "--path",
        default="/",
        help="Path to list (default: /)",
    )

    args = parser.parse_args()

    print("=" * 60)
    print("THREAD POOL EXHAUSTION TEST")
    print("=" * 60)
    print(f"Python default thread pool: {min(32, (os.cpu_count() or 1) + 4)} workers")
    print(f"Concurrent requests: {args.requests}")
    print(f"Timeout: {args.timeout}s")

    if args.use_async:
        results = asyncio.run(
            test_async_thread_exhaustion(
                num_requests=args.requests,
                timeout=args.timeout,
            )
        )
    elif args.in_process:
        results = test_in_process_thread_exhaustion(
            num_requests=args.requests,
            timeout=args.timeout,
        )
    elif args.url:
        results = test_http_concurrent_requests(
            base_url=args.url,
            num_requests=args.requests,
            timeout=args.timeout,
            path=args.path,
        )
    else:
        print("\nERROR: Specify --url, --in-process, or --async")
        parser.print_help()
        sys.exit(1)

    print(results.summary())

    # Determine if exhaustion occurred
    if results.timed_out > 0:
        print("\n*** THREAD POOL EXHAUSTION DETECTED ***")
        print(f"{results.timed_out} requests timed out waiting for threads")
        sys.exit(1)
    elif results.durations and max(results.durations) > 10:
        print("\n*** POTENTIAL EXHAUSTION: Some requests took >10s ***")
        sys.exit(1)
    else:
        print("\n*** No exhaustion detected in this test run ***")
        sys.exit(0)


if __name__ == "__main__":
    main()
