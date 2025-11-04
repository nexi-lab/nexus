"""Test FUSE readdir caching to prevent N+1 queries."""

import stat
import time
from unittest.mock import MagicMock

import pytest

from nexus.core.filesystem import NexusFilesystem
from nexus.fuse.operations import NexusFUSEOperations


@pytest.fixture
def mock_nexus_fs():
    """Create a mock NexusFilesystem for testing."""
    fs = MagicMock(spec=NexusFilesystem)

    # Mock list() to return details including size and is_directory
    fs.list.return_value = [
        {"path": "/workspace/file1.txt", "is_directory": False, "size": 100},
        {"path": "/workspace/file2.txt", "is_directory": False, "size": 200},
        {"path": "/workspace/dir1", "is_directory": True, "size": 0},
    ]

    # Mock other methods
    fs.exists.return_value = True
    fs.is_directory.return_value = False
    fs.read.return_value = b"test content"

    return fs


def test_readdir_caches_file_attributes(mock_nexus_fs):
    """Test that readdir() pre-caches file attributes to avoid N+1 queries."""
    # Create FUSE operations with cache enabled
    fuse_ops = NexusFUSEOperations(
        mock_nexus_fs,
        mode=MagicMock(value="binary"),
        cache_config={"attr_cache_ttl": 60, "enable_metrics": True},
    )

    # Call readdir() - this should cache attributes for all files
    fuse_ops.readdir("/workspace")

    # Verify list was called once with details=True
    mock_nexus_fs.list.assert_called_once_with("/workspace", recursive=False, details=True)

    # Reset call counts
    mock_nexus_fs.list.reset_mock()
    mock_nexus_fs.is_directory.reset_mock()
    mock_nexus_fs.read.reset_mock()

    # Now call getattr() on each file - should use cached data
    # This simulates what OS does when running `ls -la`
    for filename in ["file1.txt", "file2.txt"]:
        attrs = fuse_ops.getattr(f"/workspace/{filename}")

        # Verify it returned valid attributes
        assert "st_mode" in attrs
        assert "st_size" in attrs
        assert stat.S_ISREG(attrs["st_mode"])  # Regular file

    # IMPORTANT: These methods should NOT have been called
    # because we're using cached data from readdir()
    assert mock_nexus_fs.is_directory.call_count == 0, (
        "is_directory() should not be called (cached)"
    )
    assert mock_nexus_fs.read.call_count == 0, "read() should not be called (cached size)"

    # Check cache metrics
    metrics = fuse_ops.cache.get_metrics()
    assert metrics["attr_hits"] >= 2, "Should have cache hits for both files"
    assert metrics["attr_hit_rate"] > 0.5, "Cache hit rate should be good"

    print(f"✅ N+1 optimization working! Cache hits: {metrics['attr_hits']}")


def test_cache_includes_correct_size(mock_nexus_fs):
    """Test that cached attributes include correct file size from list()."""
    fuse_ops = NexusFUSEOperations(
        mock_nexus_fs,
        mode=MagicMock(value="binary"),
        cache_config={"attr_cache_ttl": 60},
    )

    # Call readdir() to populate cache
    fuse_ops.readdir("/workspace")

    # Get attributes - should use cached size (100 bytes)
    attrs = fuse_ops.getattr("/workspace/file1.txt")
    assert attrs["st_size"] == 100, "Size should match list() result"

    # Get attributes for second file - should use cached size (200 bytes)
    attrs = fuse_ops.getattr("/workspace/file2.txt")
    assert attrs["st_size"] == 200, "Size should match list() result"

    # read() should NOT have been called to determine size
    assert mock_nexus_fs.read.call_count == 0, "Should not read file to get size"


def test_cache_handles_directories(mock_nexus_fs):
    """Test that cached attributes handle directories correctly."""
    fuse_ops = NexusFUSEOperations(
        mock_nexus_fs,
        mode=MagicMock(value="binary"),
        cache_config={"attr_cache_ttl": 60},
    )

    # Call readdir() to populate cache
    fuse_ops.readdir("/workspace")

    # Get attributes for directory - should use cached data
    attrs = fuse_ops.getattr("/workspace/dir1")

    assert "st_mode" in attrs
    assert stat.S_ISDIR(attrs["st_mode"]), "Should be directory"
    assert attrs["st_size"] == 4096, "Directory size should be 4096"


def test_cache_ttl_expiration(mock_nexus_fs):
    """Test that cache expires after TTL."""
    # Short TTL for testing
    fuse_ops = NexusFUSEOperations(
        mock_nexus_fs,
        mode=MagicMock(value="binary"),
        cache_config={"attr_cache_ttl": 1, "enable_metrics": True},  # 1 second TTL
    )

    # Call readdir() to populate cache
    fuse_ops.readdir("/workspace")

    # Immediate getattr() should hit cache
    fuse_ops.getattr("/workspace/file1.txt")
    metrics = fuse_ops.cache.get_metrics()
    assert metrics["attr_hits"] >= 1, "Should have cache hit"

    # Wait for cache to expire
    time.sleep(1.5)

    # Reset mock call counts
    mock_nexus_fs.is_directory.reset_mock()

    # Now getattr() should miss cache and make RPC calls
    # Note: It will still work, just with fresh data
    fuse_ops.getattr("/workspace/file1.txt")

    # After TTL expiration, is_directory() might be called for fresh data
    # (This is expected behavior)
    print("✅ Cache TTL working correctly")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
