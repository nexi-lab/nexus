#!/usr/bin/env python3
"""Test FUSE mount integration with Rust daemon."""

import contextlib
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from nexus.fuse.rust_client import RustFUSEClient  # noqa: E402


def test_rust_client():
    """Test that Rust client can be instantiated and works."""
    print("=" * 60)
    print("Testing Rust FUSE Client Integration")
    print("=" * 60)

    # Initialize Rust client (spawns daemon)
    print("\n1. Initializing Rust FUSE client...")
    client = RustFUSEClient(
        nexus_url="http://localhost:2026", api_key="sk-test-key-123", agent_id=None
    )
    print(f"✓ Daemon started, socket: {client.socket_path}")
    time.sleep(0.5)  # Give daemon time to initialize

    # Clean up any leftover files from previous test runs
    print("\n0. Cleaning up previous test runs...")
    with contextlib.suppress(Exception):
        client.delete("/rust-test.txt")
    with contextlib.suppress(Exception):
        client.delete("/rust-testdir/renamed.txt")
    with contextlib.suppress(Exception):
        client.delete("/rust-testdir")
    print("✓ Cleanup complete")

    # Test write
    print("\n2. Testing write operation...")
    test_content = b"Hello from Rust FUSE integration test!"
    client.write("/rust-test.txt", test_content)
    print("✓ Write succeeded")

    # Test read
    print("\n3. Testing read operation...")
    content = client.read("/rust-test.txt")
    assert content == test_content, f"Content mismatch: {content!r} != {test_content!r}"
    print(f"✓ Read succeeded: {content.decode()}")

    # Test list
    print("\n4. Testing list operation...")
    entries = client.list("/")
    print(f"✓ List succeeded: {[e.name for e in entries]}")
    assert any(e.name == "rust-test.txt" for e in entries), "Test file not in listing"

    # Test stat
    print("\n5. Testing stat operation...")
    metadata = client.stat("/rust-test.txt")
    print(f"✓ Stat succeeded: size={metadata.size}, is_dir={metadata.is_directory}")
    assert metadata.size == len(test_content), (
        f"Size mismatch: {metadata.size} != {len(test_content)}"
    )

    # Test mkdir
    print("\n6. Testing mkdir operation...")
    client.mkdir("/rust-testdir")
    print("✓ Mkdir succeeded")

    # Test rename
    print("\n7. Testing rename operation...")
    client.rename("/rust-test.txt", "/rust-testdir/renamed.txt")
    print("✓ Rename succeeded")

    # Verify rename
    print("\n8. Verifying rename...")
    content = client.read("/rust-testdir/renamed.txt")
    assert content == test_content, "Content changed after rename"
    print("✓ Rename verified")

    # Test delete
    print("\n9. Testing delete operation...")
    client.delete("/rust-testdir/renamed.txt")
    print("✓ Delete succeeded")

    # Cleanup
    print("\n10. Cleaning up...")
    with contextlib.suppress(Exception):
        client.delete("/rust-testdir")
    client.close()
    print("✓ Client closed")

    print("\n" + "=" * 60)
    print("✅ ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    test_rust_client()
