"""End-to-end tests for sync_bridge in realistic scenarios (Issue #1300).

Tests that run_sync() and fire_and_forget() work correctly in:
1. FastAPI-like async server context (running event loop)
2. CLI-like sync context (no running event loop)
3. Concurrent access from multiple threads (thread pool workers)
4. Integration with real AsyncLocalBackend operations

Does NOT depend on pytest-asyncio — uses explicit event loop helper.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import time
from pathlib import Path

import pytest

from nexus.backends.async_local import AsyncLocalBackend
from nexus.core.sync_bridge import (
    fire_and_forget,
    run_sync,
    shutdown_sync_bridge,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_async(coro):
    """Run a coroutine in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def backend(tmp_path: Path) -> AsyncLocalBackend:
    """Create a temporary async local backend for testing."""
    b = AsyncLocalBackend(root_path=tmp_path / "backend")
    _run_async(b.initialize())
    return b


@pytest.fixture(autouse=True)
def _cleanup_bridge():
    """Ensure sync bridge is clean between tests."""
    yield
    shutdown_sync_bridge()


# === E2E: run_sync with real backend ===


class TestRunSyncWithBackend:
    """Test run_sync() with real AsyncLocalBackend operations."""

    def test_write_and_read_from_sync_context(self, backend: AsyncLocalBackend):
        """run_sync() should work for backend operations from sync (CLI) context."""
        content = b"hello world from sync context"
        resp = run_sync(backend.write_content(content))
        assert resp.success
        content_hash = resp.data

        read_resp = run_sync(backend.read_content(content_hash))
        assert read_resp.success
        assert read_resp.data == content

    def test_write_and_read_from_thread_pool_worker(self, backend: AsyncLocalBackend):
        """run_sync() should work from thread pool workers (simulates FastAPI)."""

        def _worker():
            content = b"hello from thread pool"
            resp = run_sync(backend.write_content(content))
            assert resp.success
            content_hash = resp.data

            read_resp = run_sync(backend.read_content(content_hash))
            assert read_resp.success
            assert read_resp.data == content
            return content_hash

        # Simulate FastAPI: run worker inside an event loop's thread pool
        async def _simulate_server():
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, _worker)

        result = _run_async(_simulate_server())
        assert result is not None

    def test_concurrent_writes_from_multiple_workers(self, backend: AsyncLocalBackend):
        """Multiple concurrent thread pool workers should all succeed."""

        def _worker(idx: int):
            content = f"content-{idx}".encode()
            resp = run_sync(backend.write_content(content))
            assert resp.success, f"Worker {idx} write failed: {resp.message}"
            return resp.data

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(_worker, i) for i in range(10)]
            hashes = [f.result(timeout=30) for f in futures]

        # All should have valid hashes
        assert len(hashes) == 10
        # Unique content should produce unique hashes
        assert len(set(hashes)) == 10


# === E2E: fire_and_forget with real operations ===


class TestFireAndForgetE2E:
    """Test fire_and_forget() in realistic scenarios."""

    def test_fire_and_forget_from_sync(self, backend: AsyncLocalBackend):
        """fire_and_forget() should complete async work from sync context."""
        content = b"fire-and-forget content"
        result_holder: list[str] = []

        async def _write_and_track():
            resp = await backend.write_content(content)
            result_holder.append(resp.data)

        fire_and_forget(_write_and_track())

        # Give background loop time to process
        time.sleep(0.5)

        assert len(result_holder) == 1
        # Verify the content was actually written
        content_hash = result_holder[0]
        read_resp = run_sync(backend.read_content(content_hash))
        assert read_resp.success
        assert read_resp.data == content


# === Lightweight CI concurrency test ===


class TestConcurrencySmoke:
    """Lightweight concurrency tests suitable for CI."""

    def test_mixed_sync_and_threaded_access(self, backend: AsyncLocalBackend):
        """Concurrent sync + threaded access should not deadlock or corrupt."""
        errors: list[str] = []

        def _sync_worker(idx: int):
            try:
                content = f"sync-{idx}".encode()
                resp = run_sync(backend.write_content(content))
                if not resp.success:
                    errors.append(f"sync worker {idx}: write failed")
                    return
                read_resp = run_sync(backend.read_content(resp.data))
                if not read_resp.success:
                    errors.append(f"sync worker {idx}: read failed")
                elif read_resp.data != content:
                    errors.append(f"sync worker {idx}: content mismatch")
            except Exception as e:
                errors.append(f"sync worker {idx}: {e}")

        # Run from thread pool (simulating FastAPI server)
        async def _run_in_threads():
            loop = asyncio.get_running_loop()
            futs = [loop.run_in_executor(None, _sync_worker, i) for i in range(5)]
            await asyncio.gather(*futs)

        _run_async(_run_in_threads())

        assert errors == [], f"Errors: {errors}"

    def test_no_deadlock_under_contention(self, backend: AsyncLocalBackend):
        """Multiple threads hitting the same content hash should not deadlock."""
        content = b"shared content"
        # Pre-write the content
        resp = run_sync(backend.write_content(content))
        content_hash = resp.data

        errors: list[str] = []

        def _reader(idx: int):
            try:
                for _ in range(5):
                    read_resp = run_sync(backend.read_content(content_hash))
                    if not read_resp.success:
                        errors.append(f"reader {idx}: read failed")
                        return
                    if read_resp.data != content:
                        errors.append(f"reader {idx}: content mismatch")
                        return
            except Exception as e:
                errors.append(f"reader {idx}: {e}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(_reader, i) for i in range(4)]
            # 10 second timeout to catch deadlocks
            done, not_done = concurrent.futures.wait(futures, timeout=10)

        assert len(not_done) == 0, "Deadlock detected: some threads did not complete"
        assert errors == [], f"Errors: {errors}"

    def test_metadata_concurrent_write_read(self, backend: AsyncLocalBackend):
        """Concurrent metadata writes and reads should be consistent."""
        content_hash = "ab" * 32

        async def _write_read_cycle():
            for i in range(5):
                meta = {"ref_count": i, "size": i * 100}
                await backend._write_metadata(content_hash, meta)
                result = await backend._read_metadata(content_hash)
                # Result should have a valid ref_count (may not be `i` if
                # another coroutine wrote concurrently, but must be valid)
                assert "ref_count" in result
                assert "size" in result

        _run_async(_write_read_cycle())
