"""Unit tests for batch write operations."""

import pytest

from nexus.backends.local import LocalBackend
from nexus.core.nexus_fs import NexusFS


@pytest.fixture
def nx(tmp_path):
    """Create NexusFS instance for testing with isolated database.

    Each test gets a fresh, isolated database to prevent test pollution.
    (Environment variable isolation is handled by the global conftest fixture)
    """
    # Use unique path for this specific test to ensure complete isolation
    import uuid

    unique_id = str(uuid.uuid4())[:8]
    data_dir = tmp_path / f"data_{unique_id}"
    db_path = data_dir / "metadata.db"

    fs = NexusFS(
        backend=LocalBackend(data_dir),
        db_path=db_path,
        auto_parse=False,
        enforce_permissions=False,  # Disable permissions for basic functionality tests
    )
    yield fs

    # Clean up
    fs.close()

    # Explicitly remove database file to prevent any leakage
    if db_path.exists():
        db_path.unlink()


def test_write_batch_basic(nx):
    """Test basic batch write functionality."""
    # Write 10 files in a batch
    files = [(f"/test/file_{i}.txt", f"content {i}".encode()) for i in range(10)]
    results = nx.write_batch(files)

    # Verify all files were written
    assert len(results) == 10
    for i, result in enumerate(results):
        assert "etag" in result
        assert "version" in result
        assert result["version"] == 1
        assert result["size"] == len(f"content {i}".encode())

    # Verify files can be read back
    for i in range(10):
        content = nx.read(f"/test/file_{i}.txt")
        assert content == f"content {i}".encode()


def test_write_batch_empty(nx):
    """Test batch write with empty list."""
    results = nx.write_batch([])
    assert results == []


def test_write_batch_single_file(nx):
    """Test batch write with single file."""
    files = [("/test/single.txt", b"single content")]
    results = nx.write_batch(files)

    assert len(results) == 1
    assert results[0]["version"] == 1
    assert nx.read("/test/single.txt") == b"single content"


def test_write_batch_version_increment(nx):
    """Test that batch write properly increments versions on update."""
    # Write initial batch
    files = [(f"/test/file_{i}.txt", b"version 1") for i in range(5)]
    results1 = nx.write_batch(files)

    # Verify version 1
    for result in results1:
        assert result["version"] == 1

    # Update same files
    files = [(f"/test/file_{i}.txt", b"version 2") for i in range(5)]
    results2 = nx.write_batch(files)

    # Verify version 2
    for result in results2:
        assert result["version"] == 2

    # Verify content is updated
    for i in range(5):
        content = nx.read(f"/test/file_{i}.txt")
        assert content == b"version 2"


def test_write_batch_version_history(nx):
    """Test that batch write creates version history entries."""
    # Write initial batch
    files = [(f"/test/file_{i}.txt", b"version 1") for i in range(3)]
    nx.write_batch(files)

    # Update batch
    files = [(f"/test/file_{i}.txt", b"version 2") for i in range(3)]
    nx.write_batch(files)

    # Verify version history exists for each file
    for i in range(3):
        versions = nx.list_versions(f"/test/file_{i}.txt")
        assert len(versions) == 2
        assert versions[0]["version"] == 2  # Newest first
        assert versions[1]["version"] == 1


def test_write_batch_deduplication(nx):
    """Test that batch write deduplicates identical content."""
    # Write multiple files with same content
    same_content = b"duplicated content"
    files = [(f"/test/dup_{i}.txt", same_content) for i in range(5)]
    results = nx.write_batch(files)

    # All should have the same etag (content hash)
    etags = [r["etag"] for r in results]
    assert len(set(etags)) == 1  # All etags are identical


def test_write_batch_mixed_new_and_update(nx):
    """Test batch write with mix of new files and updates."""
    # Create some files first
    nx.write("/test/existing_1.txt", b"old content 1")
    nx.write("/test/existing_2.txt", b"old content 2")

    # Batch with mix of new and existing files
    files = [
        ("/test/existing_1.txt", b"new content 1"),  # Update
        ("/test/new_1.txt", b"new file 1"),  # New
        ("/test/existing_2.txt", b"new content 2"),  # Update
        ("/test/new_2.txt", b"new file 2"),  # New
    ]
    results = nx.write_batch(files)

    # Check versions
    assert results[0]["version"] == 2  # existing_1 updated
    assert results[1]["version"] == 1  # new_1 created
    assert results[2]["version"] == 2  # existing_2 updated
    assert results[3]["version"] == 1  # new_2 created

    # Verify content
    assert nx.read("/test/existing_1.txt") == b"new content 1"
    assert nx.read("/test/new_1.txt") == b"new file 1"
    assert nx.read("/test/existing_2.txt") == b"new content 2"
    assert nx.read("/test/new_2.txt") == b"new file 2"


def test_write_batch_permissions_preserved(nx):
    """Test that batch write preserves metadata for existing files."""
    # Create file
    nx.write("/test/file.txt", b"content")
    meta1 = nx.metadata.get("/test/file.txt")

    # Update in batch
    files = [("/test/file.txt", b"updated content")]
    nx.write_batch(files)

    # Verify metadata fields are preserved/updated correctly
    meta2 = nx.metadata.get("/test/file.txt")
    assert meta2.path == meta1.path
    assert meta2.backend_name == meta1.backend_name
    # Content changed, so etag should be different
    assert meta2.etag != meta1.etag
    # Version should increment
    assert meta2.version == meta1.version + 1


def test_write_batch_atomic(nx):
    """Test that batch write is atomic - all or nothing."""
    # This test verifies the transaction behavior by checking that
    # if the batch write completes, all files are written
    files = [(f"/test/atomic_{i}.txt", f"content {i}".encode()) for i in range(20)]
    results = nx.write_batch(files)

    # All files should be written
    assert len(results) == 20

    # All files should be readable
    for i in range(20):
        assert nx.exists(f"/test/atomic_{i}.txt")


def test_write_batch_result_order(nx):
    """Test that results are returned in same order as input."""
    files = [
        ("/test/z.txt", b"z content"),
        ("/test/a.txt", b"a content"),
        ("/test/m.txt", b"m content"),
    ]
    results = nx.write_batch(files)

    # Results should match input order, not alphabetical
    assert len(results) == 3
    # Verify by checking sizes which are unique
    assert results[0]["size"] == len(b"z content")
    assert results[1]["size"] == len(b"a content")
    assert results[2]["size"] == len(b"m content")


@pytest.mark.skip(reason="Flaky - timing varies in CI environments")
def test_write_batch_performance_vs_individual(nx):
    """Test that batch write is faster than individual writes."""
    import time

    num_files = 50
    content = b"x" * 1024  # 1KB

    # Measure individual writes
    start = time.time()
    for i in range(num_files):
        nx.write(f"/test/individual_{i}.txt", content)
    individual_time = time.time() - start

    # Measure batch write
    files = [(f"/test/batch_{i}.txt", content) for i in range(num_files)]
    start = time.time()
    nx.write_batch(files)
    batch_time = time.time() - start

    # Batch should be significantly faster
    # Use 1.5x threshold to account for system variability and overhead
    print(f"\nIndividual: {individual_time:.3f}s, Batch: {batch_time:.3f}s")
    print(f"Speedup: {individual_time / batch_time:.1f}x")
    assert batch_time < individual_time * 0.67  # At least 1.5x faster


def test_write_batch_large_batch(nx):
    """Test batch write with large number of files."""
    # Write 100 files
    files = [(f"/test/large_{i}.txt", f"content {i}".encode()) for i in range(100)]
    results = nx.write_batch(files)

    assert len(results) == 100
    # Spot check a few files
    assert nx.read("/test/large_0.txt") == b"content 0"
    assert nx.read("/test/large_50.txt") == b"content 50"
    assert nx.read("/test/large_99.txt") == b"content 99"


def test_write_batch_different_sizes(nx):
    """Test batch write with files of different sizes."""
    files = [
        ("/test/tiny.txt", b"x"),
        ("/test/small.txt", b"x" * 100),
        ("/test/medium.txt", b"x" * 10000),
        ("/test/large.txt", b"x" * 100000),
    ]
    results = nx.write_batch(files)

    assert results[0]["size"] == 1
    assert results[1]["size"] == 100
    assert results[2]["size"] == 10000
    assert results[3]["size"] == 100000


def test_write_batch_creates_parent_dirs(nx):
    """Test that batch write creates implicit parent directories."""
    files = [
        ("/a/b/c/file1.txt", b"content 1"),
        ("/a/b/d/file2.txt", b"content 2"),
        ("/a/e/file3.txt", b"content 3"),
    ]
    nx.write_batch(files)

    # All files should be readable
    assert nx.read("/a/b/c/file1.txt") == b"content 1"
    assert nx.read("/a/b/d/file2.txt") == b"content 2"
    assert nx.read("/a/e/file3.txt") == b"content 3"

    # Parent directories should exist implicitly
    assert nx.exists("/a")
    assert nx.exists("/a/b")
    assert nx.exists("/a/b/c")
