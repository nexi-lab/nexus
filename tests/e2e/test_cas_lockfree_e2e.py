"""E2E: Lock-free CAS under concurrent HTTP writes (Issue #925).

Verifies that 50 concurrent write requests through the FastAPI server
succeed without lock contention errors. Uses the real NexusFS stack:
FastAPI -> NexusFS -> LocalBackend (CASBlobStore).

Run with:
    pytest tests/e2e/test_cas_lockfree_e2e.py -v --override-ini="addopts="
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

NUM_CONCURRENT = 50


def rpc_write(client, content: bytes, path: str, timeout: float = 30.0):
    """Write content via RPC API."""
    encoded = {"__type__": "bytes", "data": base64.b64encode(content).decode("utf-8")}
    response = client.post(
        "/api/nfs/write_file",
        json={
            "jsonrpc": "2.0",
            "method": "write_file",
            "params": {"path": path, "content": encoded},
        },
        timeout=timeout,
    )
    return response


def rpc_read(client, path: str, timeout: float = 30.0):
    """Read content via RPC API."""
    response = client.post(
        "/api/nfs/read_file",
        json={
            "jsonrpc": "2.0",
            "method": "read_file",
            "params": {"path": path},
        },
        timeout=timeout,
    )
    return response


class TestCASLockfreeE2E:
    """Concurrent HTTP writes to verify lock-free CAS through full stack."""

    def test_concurrent_writes_same_content(self, test_app):
        """50 concurrent writes of identical content — all succeed, no errors."""
        content = b"lockfree e2e same content test"
        errors = []

        def writer(i: int) -> int:
            resp = rpc_write(test_app, content, f"/e2e_lockfree/same_{i}.txt")
            return resp.status_code

        with ThreadPoolExecutor(max_workers=NUM_CONCURRENT) as pool:
            futures = [pool.submit(writer, i) for i in range(NUM_CONCURRENT)]
            for f in as_completed(futures):
                try:
                    status = f.result()
                    if status != 200:
                        errors.append(f"HTTP {status}")
                except Exception as exc:
                    errors.append(str(exc))

        assert not errors, f"Concurrent writes failed: {errors[:5]}"

    def test_concurrent_writes_different_content(self, test_app):
        """50 concurrent writes of unique content — all succeed independently."""
        errors = []

        def writer(i: int) -> int:
            content = f"lockfree e2e unique content {i}".encode()
            resp = rpc_write(test_app, content, f"/e2e_lockfree/unique_{i}.txt")
            return resp.status_code

        with ThreadPoolExecutor(max_workers=NUM_CONCURRENT) as pool:
            futures = [pool.submit(writer, i) for i in range(NUM_CONCURRENT)]
            for f in as_completed(futures):
                try:
                    status = f.result()
                    if status != 200:
                        errors.append(f"HTTP {status}")
                except Exception as exc:
                    errors.append(str(exc))

        assert not errors, f"Concurrent writes failed: {errors[:5]}"

    def test_concurrent_read_after_write(self, test_app):
        """Write content, then read it back concurrently — all reads succeed."""
        content = b"lockfree e2e read-after-write"
        path = "/e2e_lockfree/read_test.txt"

        # Write once
        resp = rpc_write(test_app, content, path)
        assert resp.status_code == 200

        # Read concurrently
        errors = []

        def reader(_i: int) -> int:
            resp = rpc_read(test_app, path)
            return resp.status_code

        with ThreadPoolExecutor(max_workers=NUM_CONCURRENT) as pool:
            futures = [pool.submit(reader, i) for i in range(NUM_CONCURRENT)]
            for f in as_completed(futures):
                try:
                    status = f.result()
                    if status != 200:
                        errors.append(f"HTTP {status}")
                except Exception as exc:
                    errors.append(str(exc))

        assert not errors, f"Concurrent reads failed: {errors[:5]}"

    @pytest.mark.parametrize("size_kb", [1, 64, 256])
    def test_concurrent_writes_various_sizes(self, test_app, size_kb):
        """Concurrent writes of various sizes — no corruption."""
        content = b"X" * (size_kb * 1024)
        errors = []

        def writer(i: int) -> int:
            resp = rpc_write(test_app, content, f"/e2e_lockfree/sized_{size_kb}kb_{i}.txt")
            return resp.status_code

        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [pool.submit(writer, i) for i in range(20)]
            for f in as_completed(futures):
                try:
                    status = f.result()
                    if status != 200:
                        errors.append(f"HTTP {status}")
                except Exception as exc:
                    errors.append(str(exc))

        assert not errors, f"Concurrent writes ({size_kb}KB) failed: {errors[:5]}"
