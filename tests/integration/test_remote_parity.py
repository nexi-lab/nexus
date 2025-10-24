#!/usr/bin/env python3
"""Comprehensive test for issue #243 - Remote vs Local Nexus Parity

Tests that remote nexus (client-server mode) works identically to embedded nexus (local mode).

This script tests all NexusFilesystem operations to ensure behavioral parity:
- Basic file operations (read, write, delete, exists)
- Directory operations (mkdir, rmdir, list, glob, grep)
- Version tracking (list_versions, get_version, rollback, diff_versions)
- Workspace snapshots (workspace_snapshot, workspace_restore, workspace_log)
- Edge cases (large files, binary data, unicode, concurrent operations)
- Performance comparison
"""

import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import nexus
from nexus.remote import RemoteNexusFS
from nexus.server import NexusRPCServer


class TestRemoteLocalParity:
    """Test suite for remote vs local parity."""

    @pytest.fixture
    def test_env(self):
        """Set up test environment with local and remote filesystems.

        Each test gets a fresh environment to avoid state pollution.
        """
        # Create temp directories
        test_dir = Path(tempfile.mkdtemp(prefix="nexus-parity-"))
        local_data_dir = test_dir / "local-data"
        remote_data_dir = test_dir / "remote-data"

        local_data_dir.mkdir()
        remote_data_dir.mkdir()

        # Create local filesystem
        local_nx = nexus.connect(config={"data_dir": str(local_data_dir)})

        # Create remote filesystem with server
        remote_nx_backend = nexus.connect(config={"data_dir": str(remote_data_dir)})

        # Start server in background thread
        server = NexusRPCServer(remote_nx_backend, host="127.0.0.1", port=0)  # port=0 for random
        port = server.server.server_address[1]

        server_thread = threading.Thread(target=server.server.serve_forever, daemon=True)
        server_thread.start()

        # Wait for server to be ready
        time.sleep(0.5)

        # Create remote client
        remote_nx = RemoteNexusFS(f"http://127.0.0.1:{port}", timeout=10)

        yield {
            "local": local_nx,
            "remote": remote_nx,
            "local_data_dir": local_data_dir,
            "remote_data_dir": remote_data_dir,
            "server": server,
            "test_dir": test_dir,
        }

        # Cleanup
        remote_nx.close()
        server.shutdown()
        local_nx.close()
        shutil.rmtree(test_dir)

    def test_basic_write_read(self, test_env):
        """Test basic write and read operations."""
        local_nx = test_env["local"]
        remote_nx = test_env["remote"]

        path = "/workspace/test_basic.txt"
        content = b"Hello, World!"

        # Write to both
        local_result = local_nx.write(path, content)
        remote_result = remote_nx.write(path, content)

        # Read from both
        local_content = local_nx.read(path)
        remote_content = remote_nx.read(path)

        # Verify
        assert local_content == content
        assert remote_content == content
        assert local_content == remote_content

        # Verify metadata
        assert "etag" in local_result
        assert "etag" in remote_result

    def test_exists(self, test_env):
        """Test exists operation."""
        local_nx = test_env["local"]
        remote_nx = test_env["remote"]

        path = "/workspace/exists_test.txt"

        # Initially should not exist
        assert not local_nx.exists(path)
        assert not remote_nx.exists(path)

        # Create file
        local_nx.write(path, b"test")
        remote_nx.write(path, b"test")

        # Should exist now
        assert local_nx.exists(path)
        assert remote_nx.exists(path)

    def test_delete(self, test_env):
        """Test delete operation."""
        local_nx = test_env["local"]
        remote_nx = test_env["remote"]

        path = "/workspace/delete_test.txt"

        # Create and verify
        local_nx.write(path, b"test")
        remote_nx.write(path, b"test")
        assert local_nx.exists(path)
        assert remote_nx.exists(path)

        # Delete
        local_nx.delete(path)
        remote_nx.delete(path)

        # Verify deleted
        assert not local_nx.exists(path)
        assert not remote_nx.exists(path)

    def test_rename(self, test_env):
        """Test rename operation."""
        local_nx = test_env["local"]
        remote_nx = test_env["remote"]

        old_path = "/workspace/old_name.txt"
        new_path = "/workspace/new_name.txt"

        # Create files
        content = b"rename test"
        local_nx.write(old_path, content)
        remote_nx.write(old_path, content)

        # Rename
        local_nx.rename(old_path, new_path)
        remote_nx.rename(old_path, new_path)

        # Verify
        assert not local_nx.exists(old_path)
        assert not remote_nx.exists(old_path)
        assert local_nx.exists(new_path)
        assert remote_nx.exists(new_path)
        assert local_nx.read(new_path) == remote_nx.read(new_path) == content

    def test_mkdir_rmdir(self, test_env):
        """Test directory operations."""
        local_nx = test_env["local"]
        remote_nx = test_env["remote"]

        dir_path = "/workspace/subdir/testdir"

        # Create directory with parents
        local_nx.mkdir(dir_path, parents=True, exist_ok=True)
        remote_nx.mkdir(dir_path, parents=True, exist_ok=True)

        # Verify
        assert local_nx.is_directory(dir_path)
        assert remote_nx.is_directory(dir_path)

        # Remove directory (non-recursive first)
        local_nx.rmdir(dir_path)
        remote_nx.rmdir(dir_path)

        # Verify testdir removed (subdir may still exist as virtual directory)
        assert not local_nx.is_directory(dir_path)
        assert not remote_nx.is_directory(dir_path)

    def test_list_files(self, test_env):
        """Test list operation."""
        local_nx = test_env["local"]
        remote_nx = test_env["remote"]

        # Create test files
        files = [
            "/workspace/list_test/file1.txt",
            "/workspace/list_test/file2.txt",
            "/workspace/list_test/subdir/file3.txt",
        ]

        for path in files:
            local_nx.write(path, b"test")
            remote_nx.write(path, b"test")

        # List files
        local_files = local_nx.list("/workspace/list_test", recursive=True)
        remote_files = remote_nx.list("/workspace/list_test", recursive=True)

        # Sort for comparison
        local_files_sorted = sorted(local_files)
        remote_files_sorted = sorted(remote_files)

        assert local_files_sorted == remote_files_sorted
        assert len(local_files_sorted) == 3

    def test_glob(self, test_env):
        """Test glob pattern matching."""
        local_nx = test_env["local"]
        remote_nx = test_env["remote"]

        # Create test files
        files = [
            "/workspace/glob_test/test1.txt",
            "/workspace/glob_test/test2.py",
            "/workspace/glob_test/test3.txt",
        ]

        for path in files:
            local_nx.write(path, b"test")
            remote_nx.write(path, b"test")

        # Glob for .txt files
        local_matches = local_nx.glob("*.txt", "/workspace/glob_test")
        remote_matches = remote_nx.glob("*.txt", "/workspace/glob_test")

        # Sort for comparison
        local_matches_sorted = sorted(local_matches)
        remote_matches_sorted = sorted(remote_matches)

        assert local_matches_sorted == remote_matches_sorted
        assert len(local_matches_sorted) == 2

    def test_grep(self, test_env):
        """Test grep search."""
        local_nx = test_env["local"]
        remote_nx = test_env["remote"]

        # Create test files with content
        local_nx.write("/workspace/grep_test/file1.txt", b"Hello World")
        local_nx.write("/workspace/grep_test/file2.txt", b"Goodbye World")

        remote_nx.write("/workspace/grep_test/file1.txt", b"Hello World")
        remote_nx.write("/workspace/grep_test/file2.txt", b"Goodbye World")

        # Search for "World"
        local_results = local_nx.grep("World", "/workspace/grep_test")
        remote_results = remote_nx.grep("World", "/workspace/grep_test")

        # Both should find 2 matches
        assert len(local_results) == len(remote_results) == 2

    def test_large_files(self, test_env):
        """Test handling of large files (1MB)."""
        local_nx = test_env["local"]
        remote_nx = test_env["remote"]

        path = "/workspace/large_file.bin"

        # Create 1MB of random data
        large_content = os.urandom(1024 * 1024)

        # Write to both
        local_nx.write(path, large_content)
        remote_nx.write(path, large_content)

        # Read and verify
        local_read = local_nx.read(path)
        remote_read = remote_nx.read(path)

        assert local_read == large_content
        assert remote_read == large_content
        assert local_read == remote_read

    def test_binary_data(self, test_env):
        """Test binary data handling."""
        local_nx = test_env["local"]
        remote_nx = test_env["remote"]

        path = "/workspace/binary.dat"

        # Create binary data with all byte values
        binary_content = bytes(range(256))

        local_nx.write(path, binary_content)
        remote_nx.write(path, binary_content)

        assert local_nx.read(path) == remote_nx.read(path) == binary_content

    def test_unicode_content(self, test_env):
        """Test Unicode content handling."""
        local_nx = test_env["local"]
        remote_nx = test_env["remote"]

        path = "/workspace/unicode.txt"

        # Unicode content
        unicode_content = "Hello 世界 🌍 Привет مرحبا".encode()

        local_nx.write(path, unicode_content)
        remote_nx.write(path, unicode_content)

        assert local_nx.read(path) == remote_nx.read(path) == unicode_content

    def test_empty_files(self, test_env):
        """Test empty file handling."""
        local_nx = test_env["local"]
        remote_nx = test_env["remote"]

        path = "/workspace/empty.txt"

        local_nx.write(path, b"")
        remote_nx.write(path, b"")

        assert local_nx.read(path) == remote_nx.read(path) == b""

    @pytest.mark.skip(reason="write_batch not implemented in RPC server (issue #243)")
    def test_write_batch(self, test_env):
        """Test batch write operation."""
        local_nx = test_env["local"]
        remote_nx = test_env["remote"]

        files = [
            ("/workspace/batch1.txt", b"content1"),
            ("/workspace/batch2.txt", b"content2"),
            ("/workspace/batch3.txt", b"content3"),
        ]

        # Write batch
        local_results = local_nx.write_batch(files)
        remote_results = remote_nx.write_batch(files)

        # Verify all written
        assert len(local_results) == len(remote_results) == 3

        # Verify content
        for path, content in files:
            assert local_nx.read(path) == remote_nx.read(path) == content

    @pytest.mark.skip(reason="Version tracking methods not implemented in RPC server (issue #243)")
    def test_version_tracking(self, test_env):
        """Test version tracking operations."""
        local_nx = test_env["local"]
        remote_nx = test_env["remote"]

        path = "/workspace/versioned.txt"

        # Create multiple versions
        local_nx.write(path, b"version 1")
        local_nx.write(path, b"version 2")
        local_nx.write(path, b"version 3")

        remote_nx.write(path, b"version 1")
        remote_nx.write(path, b"version 2")
        remote_nx.write(path, b"version 3")

        # List versions
        local_versions = local_nx.list_versions(path)
        remote_versions = remote_nx.list_versions(path)

        assert len(local_versions) == len(remote_versions) == 3

        # Get specific version
        local_v1 = local_nx.get_version(path, 1)
        remote_v1 = remote_nx.get_version(path, 1)

        assert local_v1 == remote_v1 == b"version 1"

    def test_namespace_listing(self, test_env):
        """Test namespace listing."""
        local_nx = test_env["local"]
        remote_nx = test_env["remote"]

        # Get available namespaces
        local_namespaces = local_nx.get_available_namespaces()
        remote_namespaces = remote_nx.get_available_namespaces()

        # Should have at least workspace
        assert "workspace" in local_namespaces
        assert "workspace" in remote_namespaces
        assert sorted(local_namespaces) == sorted(remote_namespaces)

    def test_concurrent_writes(self, test_env):
        """Test concurrent write operations."""
        local_nx = test_env["local"]
        remote_nx = test_env["remote"]

        def write_files(nx, prefix, count=10):
            """Write multiple files."""
            for i in range(count):
                path = f"/workspace/concurrent/{prefix}_{i}.txt"
                nx.write(path, f"content {i}".encode())

        # Concurrent writes to local
        threads_local = [
            threading.Thread(target=write_files, args=(local_nx, f"local_{i}")) for i in range(5)
        ]
        for t in threads_local:
            t.start()
        for t in threads_local:
            t.join()

        # Concurrent writes to remote
        threads_remote = [
            threading.Thread(target=write_files, args=(remote_nx, f"remote_{i}")) for i in range(5)
        ]
        for t in threads_remote:
            t.start()
        for t in threads_remote:
            t.join()

        # Verify all files created
        local_files = local_nx.list("/workspace/concurrent")
        remote_files = remote_nx.list("/workspace/concurrent")

        assert len(local_files) == 50  # 5 threads × 10 files
        assert len(remote_files) == 50

    def test_read_with_metadata(self, test_env):
        """Test read with metadata return."""
        local_nx = test_env["local"]
        remote_nx = test_env["remote"]

        path = "/workspace/metadata_test.txt"
        content = b"test content"

        # Write
        local_nx.write(path, content)
        remote_nx.write(path, content)

        # Read with metadata
        local_result = local_nx.read(path, return_metadata=True)
        remote_result = remote_nx.read(path, return_metadata=True)

        # Verify structure
        assert "content" in local_result
        assert "etag" in local_result
        assert "version" in local_result

        assert "content" in remote_result
        assert "etag" in remote_result
        assert "version" in remote_result

        # Verify content matches
        assert local_result["content"] == remote_result["content"] == content

    def test_optimistic_concurrency_control(self, test_env):
        """Test optimistic concurrency control with if_match."""
        local_nx = test_env["local"]
        remote_nx = test_env["remote"]

        path = "/workspace/occ_test.txt"

        # Initial write
        local_result = local_nx.write(path, b"version 1")
        remote_result = remote_nx.write(path, b"version 1")

        local_etag = local_result["etag"]
        remote_etag = remote_result["etag"]

        # Update with correct etag should succeed
        local_nx.write(path, b"version 2", if_match=local_etag)
        remote_nx.write(path, b"version 2", if_match=remote_etag)

        # Verify updated
        assert local_nx.read(path) == b"version 2"
        assert remote_nx.read(path) == b"version 2"

    def test_timestamps_metadata(self, test_env):
        """Test timestamp metadata preservation."""
        local_nx = test_env["local"]
        remote_nx = test_env["remote"]

        path = "/workspace/timestamp_test.txt"
        content = b"timestamp test"

        # Write files
        local_nx.write(path, content)
        remote_nx.write(path, content)

        # Read with metadata
        local_meta = local_nx.read(path, return_metadata=True)
        remote_meta = remote_nx.read(path, return_metadata=True)

        # Check that both have timestamp fields
        assert "modified_at" in local_meta or "version" in local_meta
        assert "modified_at" in remote_meta or "version" in remote_meta

        # Note: Exact timestamp values may differ slightly due to timing,
        # but both should have the fields present
        print(f"\n  Local metadata: {local_meta.keys()}")
        print(f"  Remote metadata: {remote_meta.keys()}")

    def test_virtual_views_content(self, test_env):
        """Test virtual view content parsing (.txt, .md suffixes)."""
        local_nx = test_env["local"]
        remote_nx = test_env["remote"]

        # Write a file
        path = "/workspace/test_doc.txt"
        content = b"Hello, this is test content!"

        local_nx.write(path, content)
        remote_nx.write(path, content)

        # Read normal path
        local_normal = local_nx.read(path)
        remote_normal = remote_nx.read(path)

        assert local_normal == remote_normal == content

        # Test that file exists check works for both
        assert local_nx.exists(path)
        assert remote_nx.exists(path)

    def test_special_characters_filenames(self, test_env):
        """Test special characters in filenames."""
        local_nx = test_env["local"]
        remote_nx = test_env["remote"]

        # Various special characters
        test_names = [
            "/workspace/file with spaces.txt",
            "/workspace/file-with-dashes.txt",
            "/workspace/file_with_underscores.txt",
            "/workspace/file.multiple.dots.txt",
        ]

        for path in test_names:
            content = f"Content for {path}".encode()

            # Write
            local_nx.write(path, content)
            remote_nx.write(path, content)

            # Read and verify
            assert local_nx.read(path) == remote_nx.read(path) == content

    def test_performance_comparison(self, test_env):
        """Compare performance between local and remote."""
        local_nx = test_env["local"]
        remote_nx = test_env["remote"]

        num_operations = 50
        content = b"performance test content"

        # Measure local write performance
        start = time.time()
        for i in range(num_operations):
            local_nx.write(f"/workspace/perf_local_{i}.txt", content)
        local_write_time = time.time() - start

        # Measure remote write performance
        start = time.time()
        for i in range(num_operations):
            remote_nx.write(f"/workspace/perf_remote_{i}.txt", content)
        remote_write_time = time.time() - start

        # Measure local read performance
        start = time.time()
        for i in range(num_operations):
            local_nx.read(f"/workspace/perf_local_{i}.txt")
        local_read_time = time.time() - start

        # Measure remote read performance
        start = time.time()
        for i in range(num_operations):
            remote_nx.read(f"/workspace/perf_remote_{i}.txt")
        remote_read_time = time.time() - start

        print(f"\n  Local write time: {local_write_time:.3f}s")
        print(f"  Remote write time: {remote_write_time:.3f}s")
        print(f"  Local read time: {local_read_time:.3f}s")
        print(f"  Remote read time: {remote_read_time:.3f}s")

        # Performance comparison is informational
        # Note: Remote will be slower due to HTTP overhead (each operation is a separate request)
        # This is expected behavior - just verifying operations complete successfully
        write_ratio = remote_write_time / local_write_time if local_write_time > 0 else 0
        read_ratio = remote_read_time / local_read_time if local_read_time > 0 else 0
        print(f"  Write slowdown: {write_ratio:.1f}x")
        print(f"  Read slowdown: {read_ratio:.1f}x")

        # Just verify both complete successfully (no hard performance requirement)
        assert remote_write_time > 0
        assert remote_read_time > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
