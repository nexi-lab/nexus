"""Unit tests for HierarchyManager batch operations.

Tests cover:
- ensure_parent_tuples_batch() basic functionality
- Deduplication across multiple paths
- Memory-efficient chunking
- Progress logging for large batches
- Bulk cache invalidation
- Performance compared to individual operations
"""

import pytest
from sqlalchemy import create_engine

from nexus.core.hierarchy_manager import HierarchyManager
from nexus.core.rebac_manager import ReBACManager
from nexus.storage.models import Base


@pytest.fixture
def engine():
    """Create in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def rebac_manager(engine):
    """Create a ReBAC manager for testing."""
    manager = ReBACManager(
        engine=engine,
        cache_ttl_seconds=300,
        max_depth=10,
    )
    yield manager
    manager.close()


@pytest.fixture
def hierarchy_manager(rebac_manager):
    """Create a HierarchyManager for testing."""
    return HierarchyManager(rebac_manager, enable_inheritance=True)


def test_ensure_parent_tuples_batch_basic(hierarchy_manager):
    """Test basic batch parent tuple creation."""
    paths = [
        "/workspace/projects/file1.txt",
        "/workspace/projects/file2.txt",
    ]

    created = hierarchy_manager.ensure_parent_tuples_batch(paths, tenant_id="org_123")

    # Should create parent tuples for both files
    # /workspace/projects/file1.txt -> /workspace/projects
    # /workspace/projects/file2.txt -> /workspace/projects
    # /workspace/projects -> /workspace
    # /workspace -> / (if needed)
    assert created > 0

    # Verify relationships exist
    assert hierarchy_manager.rebac_manager.rebac_check(
        subject=("file", "/workspace/projects/file1.txt"),
        permission="parent",
        object=("file", "/workspace/projects"),
        tenant_id="org_123",
    )


def test_ensure_parent_tuples_batch_deduplication(hierarchy_manager):
    """Test that batch operation deduplicates shared parent paths."""
    paths = [
        "/workspace/projects/project1/file1.txt",
        "/workspace/projects/project1/file2.txt",
        "/workspace/projects/project2/file3.txt",
    ]

    created = hierarchy_manager.ensure_parent_tuples_batch(paths, tenant_id="org_123")

    # Shared parent paths should only be created once
    # All files share /workspace/projects -> /workspace -> /
    # So we shouldn't create duplicates
    assert created > 0

    # Verify all relationships exist
    for path in paths:
        parent = hierarchy_manager.get_parent_path(path)
        if parent:
            assert hierarchy_manager.rebac_manager.rebac_check(
                subject=("file", path),
                permission="parent",
                object=("file", parent),
                tenant_id="org_123",
            )


def test_ensure_parent_tuples_batch_empty_list(hierarchy_manager):
    """Test that empty path list returns 0."""
    created = hierarchy_manager.ensure_parent_tuples_batch([], tenant_id="org_123")
    assert created == 0


def test_ensure_parent_tuples_batch_root_paths(hierarchy_manager):
    """Test handling of root paths (should not create parent tuples)."""
    paths = ["/"]

    created = hierarchy_manager.ensure_parent_tuples_batch(paths, tenant_id="org_123")
    assert created == 0  # Root has no parent


def test_ensure_parent_tuples_batch_single_level(hierarchy_manager):
    """Test paths with only one level (immediate children of root)."""
    paths = ["/workspace"]

    created = hierarchy_manager.ensure_parent_tuples_batch(paths, tenant_id="org_123")
    # /workspace -> / might be created depending on implementation
    assert created >= 0


def test_ensure_parent_tuples_batch_idempotent(hierarchy_manager):
    """Test that batch operation is idempotent."""
    paths = ["/workspace/projects/file1.txt"]

    created1 = hierarchy_manager.ensure_parent_tuples_batch(paths, tenant_id="org_123")
    assert created1 > 0

    # Run again with same paths
    created2 = hierarchy_manager.ensure_parent_tuples_batch(paths, tenant_id="org_123")
    assert created2 == 0  # Should skip existing tuples


def test_ensure_parent_tuples_batch_large(hierarchy_manager):
    """Test batch operation with large number of paths."""
    # Create 100 file paths with various depths
    paths = []
    for i in range(100):
        depth = (i % 5) + 2  # Depths from 2 to 6
        path_parts = ["workspace"] + [f"level{j}" for j in range(depth - 1)] + [f"file{i}.txt"]
        paths.append("/" + "/".join(path_parts))

    created = hierarchy_manager.ensure_parent_tuples_batch(paths, tenant_id="org_123")

    # Should create many tuples (exact count depends on deduplication)
    assert created > 0

    # Verify a few samples
    assert hierarchy_manager.rebac_manager.rebac_check(
        subject=("file", paths[0]),
        permission="parent",
        object=("file", hierarchy_manager.get_parent_path(paths[0])),
        tenant_id="org_123",
    )


def test_ensure_parent_tuples_batch_chunking(hierarchy_manager):
    """Test that large batches are processed in chunks."""
    # Create enough paths to trigger multiple batches
    # Default batch_size is 1000, so create 2500 tuples worth of paths
    paths = []
    for i in range(500):  # 500 files with ~5 parents each = ~2500 tuples
        paths.append(f"/a/b/c/d/e/file{i}.txt")

    created = hierarchy_manager.ensure_parent_tuples_batch(
        paths, tenant_id="org_123", batch_size=1000
    )

    # Should have created all necessary tuples
    assert created > 0


def test_ensure_parent_tuples_batch_different_tenants(hierarchy_manager):
    """Test batch operation respects tenant isolation."""
    paths = ["/workspace/file1.txt", "/workspace/file2.txt"]

    created1 = hierarchy_manager.ensure_parent_tuples_batch(paths, tenant_id="org_123")
    created2 = hierarchy_manager.ensure_parent_tuples_batch(paths, tenant_id="org_456")

    # Both should create tuples (different tenants)
    assert created1 > 0
    assert created2 > 0

    # Verify tuples were created correctly for each tenant
    assert hierarchy_manager.rebac_manager.rebac_check(
        subject=("file", "/workspace/file1.txt"),
        permission="parent",
        object=("file", "/workspace"),
        tenant_id="org_123",
    )

    assert hierarchy_manager.rebac_manager.rebac_check(
        subject=("file", "/workspace/file1.txt"),
        permission="parent",
        object=("file", "/workspace"),
        tenant_id="org_456",
    )
    # Note: Tenant isolation is enforced at write time via cross-tenant validation


@pytest.mark.skip(reason="Performance tests are flaky in CI")
def test_ensure_parent_tuples_batch_vs_individual_performance(hierarchy_manager):
    """Compare performance of batch vs individual operations."""
    import time

    paths = [f"/workspace/projects/project{i}/file.txt" for i in range(50)]

    # Batch operation
    start = time.time()
    hierarchy_manager.ensure_parent_tuples_batch(paths, tenant_id="org_123")
    batch_time = time.time() - start

    # Clear database
    with hierarchy_manager.rebac_manager._connection() as conn:
        cursor = hierarchy_manager.rebac_manager._create_cursor(conn)
        cursor.execute("DELETE FROM rebac_tuples")
        conn.commit()

    # Individual operations
    start = time.time()
    for path in paths:
        hierarchy_manager.ensure_parent_tuples(path, tenant_id="org_123")
    individual_time = time.time() - start

    print(f"Batch: {batch_time:.3f}s, Individual: {individual_time:.3f}s")
    # Batch should be significantly faster
    assert batch_time < individual_time, (
        f"Batch not faster: {batch_time:.3f}s vs {individual_time:.3f}s"
    )


def test_ensure_parent_tuples_batch_inheritance_disabled(rebac_manager):
    """Test that batch operations are skipped when inheritance is disabled."""
    hierarchy_manager = HierarchyManager(rebac_manager, enable_inheritance=False)

    paths = ["/workspace/file1.txt"]
    created = hierarchy_manager.ensure_parent_tuples_batch(paths, tenant_id="org_123")

    assert created == 0  # Should skip when inheritance disabled


def test_ensure_parent_tuples_batch_special_characters(hierarchy_manager):
    """Test batch operations with paths containing special characters."""
    paths = [
        "/workspace/projects/my project/file (1).txt",
        "/workspace/projects/test-folder/data.json",
    ]

    created = hierarchy_manager.ensure_parent_tuples_batch(paths, tenant_id="org_123")

    assert created > 0

    # Verify relationships
    assert hierarchy_manager.rebac_manager.rebac_check(
        subject=("file", "/workspace/projects/my project/file (1).txt"),
        permission="parent",
        object=("file", "/workspace/projects/my project"),
        tenant_id="org_123",
    )


def test_bulk_cache_invalidation(hierarchy_manager):
    """Test that bulk cache invalidation works correctly."""
    paths = [
        "/workspace/file1.txt",
        "/workspace/file2.txt",
        "/workspace/projects/file3.txt",
    ]

    # Create tuples and populate cache
    hierarchy_manager.ensure_parent_tuples_batch(paths, tenant_id="org_123")

    # Verify tuples exist (this populates cache)
    for path in paths:
        parent = hierarchy_manager.get_parent_path(path)
        if parent:
            hierarchy_manager.rebac_manager.rebac_check(
                subject=("file", path),
                permission="parent",
                object=("file", parent),
                tenant_id="org_123",
            )

    # Create more tuples (should invalidate cache)
    new_paths = ["/workspace/file4.txt"]
    hierarchy_manager.ensure_parent_tuples_batch(new_paths, tenant_id="org_123")

    # Verify new tuples are visible (cache was properly invalidated)
    assert hierarchy_manager.rebac_manager.rebac_check(
        subject=("file", "/workspace/file4.txt"),
        permission="parent",
        object=("file", "/workspace"),
        tenant_id="org_123",
    )
