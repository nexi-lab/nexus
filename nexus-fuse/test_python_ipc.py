#!/usr/bin/env python3
"""Test script for Python-Rust IPC communication.

This script tests the RustFUSEClient by performing basic file operations
against a live Nexus server via the Rust daemon.
"""

import sys
from pathlib import Path

# Add nexus to Python path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nexus.fuse.rust_client import RustFUSEClient


def main() -> None:
    print("🧪 Testing Python → Rust IPC communication\n")

    # Create client (spawns Rust daemon)
    rust_binary = str(Path(__file__).parent / "target/debug/nexus-fuse")

    print(f"1. Starting Rust daemon ({rust_binary})...")
    with RustFUSEClient(
        nexus_url="http://localhost:2026",
        api_key="sk-test-key-123",
        rust_binary=rust_binary,
    ) as client:
        print("   ✓ Daemon started\n")

        # Test 1: Write file
        print("2. Writing test file...")
        test_content = b"Hello from Python via Rust!"
        client.sys_write("/python-rust-test.txt", test_content)
        print("   ✓ Write successful\n")

        # Test 2: Read file
        print("3. Reading test file...")
        content = client.sys_read("/python-rust-test.txt")
        assert content == test_content, f"Content mismatch: {content!r} != {test_content!r}"
        print(f"   ✓ Read successful: {content.decode()!r}\n")

        # Test 3: Stat file
        print("4. Getting file metadata...")
        metadata = client.stat("/python-rust-test.txt")
        print(f"   ✓ Stat successful: size={metadata.size}, is_dir={metadata.is_directory}\n")
        assert metadata.size == len(test_content), (
            f"Size mismatch: {metadata.size} != {len(test_content)}"
        )
        assert not metadata.is_directory, "File should not be a directory"

        # Test 4: List directory
        print("5. Listing directory...")
        files = client.sys_readdir("/")
        print(f"   ✓ List successful: {len(files)} files\n")
        file_names = [f.name for f in files]
        assert "python-rust-test.txt" in file_names, f"Test file not found in: {file_names}"

        # Test 5: Exists check
        print("6. Checking file exists...")
        assert client.sys_access("/python-rust-test.txt"), "File should exist"
        assert not client.sys_access("/nonexistent.txt"), "Nonexistent file should not exist"
        print("   ✓ Exists check successful\n")

        # Test 6: Rename file
        print("7. Renaming file...")
        client.sys_rename("/python-rust-test.txt", "/python-rust-renamed.txt")
        assert client.sys_access("/python-rust-renamed.txt"), "Renamed file should exist"
        assert not client.sys_access("/python-rust-test.txt"), "Old file should not exist"
        print("   ✓ Rename successful\n")

        # Test 7: Delete file
        print("8. Deleting file...")
        client.sys_unlink("/python-rust-renamed.txt")
        assert not client.sys_access("/python-rust-renamed.txt"), "Deleted file should not exist"
        print("   ✓ Delete successful\n")

        # Test 8: Error handling (404)
        print("9. Testing error handling (404)...")
        try:
            client.sys_read("/nonexistent-file.txt")
            print("   ✗ Should have raised OSError")
            sys.exit(1)
        except OSError as e:
            assert e.errno == 2, f"Expected ENOENT (2), got {e.errno}"  # ENOENT
            print(f"   ✓ Correctly raised OSError: errno={e.errno}, msg={e.strerror!r}\n")

    print("🎉 All tests passed! Python ↔ Rust IPC working correctly\n")


if __name__ == "__main__":
    main()
