#!/usr/bin/env python3
"""End-to-end test for the cache_warm JSON-RPC method (Issue #4055).

Prerequisites: a Nexus server running at http://localhost:2026 — same as the
sibling test_python_ipc.py. Run via:

    python nexus-fuse/test_cache_warm.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nexus.fuse.rust_client import RustFUSEClient


def main() -> int:
    print("🧪 Testing cache_warm round-trip\n")
    rust_binary = str(Path(__file__).parent / "target/debug/nexus-fuse")

    # Use the SAME constructor kwargs as the sibling test_python_ipc.py.
    with RustFUSEClient(
        nexus_url="http://localhost:2026",
        api_key="sk-test-key-123",
        rust_binary=rust_binary,
    ) as client:
        # Seed: create three small files via sys_write so the backend has content.
        client.sys_write("/hyd_a.txt", b"alpha")
        client.sys_write("/hyd_b.txt", b"beta")
        client.sys_write("/hyd_big.bin", b"x" * (200 * 1024))  # over threshold

        stats = client.cache_warm("/")
        print(f"hydration stats: {stats}")
        assert stats["admitted_count"] >= 2, f"expected >=2 admits, got {stats}"
        assert stats["skipped_size"] >= 1, f"expected >=1 skip, got {stats}"

        # Re-running cache_warm should report skipped_warm for the
        # previously-admitted entries.
        stats2 = client.cache_warm("/")
        print(f"second hydration stats: {stats2}")
        assert stats2["skipped_warm"] >= 2, f"expected warm skips, got {stats2}"

    print("✅ cache_warm e2e test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
