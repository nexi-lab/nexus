"""Comprehensive async tests for AsyncSQLAlchemyMetadataStore (Issue #940).

These tests verify that the async metadata store behaves correctly for all
core operations. TDD-driven tests created BEFORE implementation.

Tests cover:
- aget(): Get file metadata asynchronously
- aput(): Store file metadata asynchronously
- adelete(): Delete file metadata (soft delete) asynchronously
- alist(): List files by prefix asynchronously
- aexists(): Check file existence asynchronously
- alist_paginated(): Paginated listing asynchronously
- Error handling and edge cases
- Cache behavior (L1 cache hits/misses)
- Concurrent operations

Performance benefits when implemented:
- Non-blocking DB operations for 10-100x throughput under concurrent load
- No thread pool exhaustion under high concurrency
- Integrates seamlessly with FastAPI's async endpoints

Reference patterns:
- AsyncReBACManager (src/nexus/core/async_rebac_manager.py)
- AsyncSemanticSearch (src/nexus/search/async_search.py)

Requirements:
- PostgreSQL running on localhost:5432 (use docker-compose or local install)
- Database: scorpio (user: scorpio, password: scorpio) or set TEST_DATABASE_URL
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nexus.core._metadata_generated import FileMetadata

# Run these PostgreSQL tests in the same xdist worker to avoid connection conflicts
pytestmark = pytest.mark.xdist_group("async_metadata_store")

# PostgreSQL test database URL
# Default: connect to local scorpio-postgres container
# Override with TEST_DATABASE_URL environment variable
TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql+asyncpg://scorpio:scorpio@localhost:5432/scorpio"
)


@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    """Create async SQLAlchemy engine with PostgreSQL for testing.

    Selectively creates only the tables needed for metadata store tests.
    """
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)

    # Import only the specific models we need
    from nexus.storage.models import (
        DirectoryEntryModel,
        FilePathModel,
        VersionHistoryModel,
    )

    async with engine.begin() as conn:
        # Create only the specific tables we need (not all models)
        tables = [
            FilePathModel.__table__,
            DirectoryEntryModel.__table__,
            VersionHistoryModel.__table__,
        ]
        for table in tables:
            await conn.run_sync(lambda sync_conn, t=table: t.create(sync_conn, checkfirst=True))

        # Clean any existing test data
        try:
            await conn.execute(
                text("TRUNCATE file_paths, directory_entries, version_history CASCADE")
            )
        except Exception:
            pass  # Tables might not exist yet on first run

    yield engine

    # Cleanup after tests
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("TRUNCATE file_paths, directory_entries, version_history CASCADE")
            )
        except Exception:
            pass

    await engine.dispose()


@pytest_asyncio.fixture
async def store(engine: AsyncEngine) -> AsyncGenerator:
    """Create AsyncSQLAlchemyMetadataStore for testing."""
    # Import here to avoid import errors before implementation exists
    from nexus.storage.async_metadata_store import AsyncSQLAlchemyMetadataStore

    store = AsyncSQLAlchemyMetadataStore(engine=engine, enable_cache=True)
    yield store


class TestAsyncGet:
    """Tests for async aget() method."""

    @pytest.mark.asyncio
    async def test_aget_existing_file(self, store) -> None:
        """Test getting metadata for an existing file."""
        # First put a file
        metadata = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/storage/abc123",
            size=1024,
            etag="sha256:abc123",
            mime_type="text/plain",
            created_at=datetime.now(UTC),
            modified_at=datetime.now(UTC),
        )
        await store.aput(metadata)

        # Then get it
        result = await store.aget("/test/file.txt")
        assert result is not None
        assert result.path == "/test/file.txt"
        assert result.backend_name == "local"
        assert result.size == 1024
        assert result.etag == "sha256:abc123"

    @pytest.mark.asyncio
    async def test_aget_nonexistent_file(self, store) -> None:
        """Test getting metadata for a file that doesn't exist."""
        result = await store.aget("/nonexistent/file.txt")
        assert result is None

    @pytest.mark.asyncio
    async def test_aget_deleted_file_returns_none(self, store) -> None:
        """Test that soft-deleted files are not returned."""
        # Put a file
        metadata = FileMetadata(
            path="/deleted/file.txt",
            backend_name="local",
            physical_path="/storage/deleted",
            size=100,
        )
        await store.aput(metadata)

        # Delete it (soft delete)
        await store.adelete("/deleted/file.txt")

        # Should return None
        result = await store.aget("/deleted/file.txt")
        assert result is None

    @pytest.mark.asyncio
    async def test_aget_with_cache_hit(self, store) -> None:
        """Test that cache is used on second get."""
        metadata = FileMetadata(
            path="/cached/file.txt",
            backend_name="local",
            physical_path="/storage/cached",
            size=512,
        )
        await store.aput(metadata)

        # First get (cache miss, populates cache)
        result1 = await store.aget("/cached/file.txt")
        assert result1 is not None

        # Second get (should hit cache)
        result2 = await store.aget("/cached/file.txt")
        assert result2 is not None
        assert result2.path == result1.path


class TestAsyncPut:
    """Tests for async aput() method."""

    @pytest.mark.asyncio
    async def test_aput_new_file(self, store) -> None:
        """Test storing metadata for a new file."""
        metadata = FileMetadata(
            path="/new/file.txt",
            backend_name="local",
            physical_path="/storage/new123",
            size=2048,
            etag="sha256:new123",
            mime_type="text/plain",
        )
        await store.aput(metadata)

        # Verify it was stored
        result = await store.aget("/new/file.txt")
        assert result is not None
        assert result.path == "/new/file.txt"
        assert result.size == 2048

    @pytest.mark.asyncio
    async def test_aput_update_existing_file(self, store) -> None:
        """Test updating metadata for an existing file increments version."""
        # Create initial file
        metadata1 = FileMetadata(
            path="/update/file.txt",
            backend_name="local",
            physical_path="/storage/v1",
            size=1000,
            etag="sha256:v1",
        )
        await store.aput(metadata1)

        # Update with new content
        metadata2 = FileMetadata(
            path="/update/file.txt",
            backend_name="local",
            physical_path="/storage/v2",
            size=2000,
            etag="sha256:v2",
        )
        await store.aput(metadata2)

        # Verify updated values
        result = await store.aget("/update/file.txt")
        assert result is not None
        assert result.size == 2000
        assert result.etag == "sha256:v2"
        assert result.version == 2  # Version incremented

    @pytest.mark.asyncio
    async def test_aput_with_tenant_id(self, store) -> None:
        """Test storing file with specific tenant_id."""
        metadata = FileMetadata(
            path="/tenant/file.txt",
            backend_name="local",
            physical_path="/storage/tenant",
            size=100,
            tenant_id="org_acme",
        )
        await store.aput(metadata)

        result = await store.aget("/tenant/file.txt")
        assert result is not None
        assert result.tenant_id == "org_acme"

    @pytest.mark.asyncio
    async def test_aput_creates_directory_index(self, store) -> None:
        """Test that put creates directory index entries (Issue #924)."""
        metadata = FileMetadata(
            path="/workspace/project/file.txt",
            backend_name="local",
            physical_path="/storage/proj",
            size=100,
        )
        await store.aput(metadata)

        # The directory index should have entries for parent directories
        # This is verified by alist with recursive=False working correctly
        # (tested in TestAsyncList)


class TestAsyncDelete:
    """Tests for async adelete() method."""

    @pytest.mark.asyncio
    async def test_adelete_existing_file(self, store) -> None:
        """Test soft-deleting an existing file."""
        metadata = FileMetadata(
            path="/todelete/file.txt",
            backend_name="local",
            physical_path="/storage/del",
            size=100,
        )
        await store.aput(metadata)

        # Delete it
        result = await store.adelete("/todelete/file.txt")
        assert result is not None
        assert result["content_hash"] is None or "path_id" in result

        # Verify it's gone
        check = await store.aget("/todelete/file.txt")
        assert check is None

    @pytest.mark.asyncio
    async def test_adelete_nonexistent_file(self, store) -> None:
        """Test deleting a file that doesn't exist returns None."""
        result = await store.adelete("/nonexistent/to/delete.txt")
        assert result is None

    @pytest.mark.asyncio
    async def test_adelete_invalidates_cache(self, store) -> None:
        """Test that delete invalidates cache."""
        metadata = FileMetadata(
            path="/cache_del/file.txt",
            backend_name="local",
            physical_path="/storage/cdel",
            size=100,
        )
        await store.aput(metadata)

        # Get to populate cache
        await store.aget("/cache_del/file.txt")

        # Delete
        await store.adelete("/cache_del/file.txt")

        # Should not return from cache
        result = await store.aget("/cache_del/file.txt")
        assert result is None


class TestAsyncList:
    """Tests for async alist() method."""

    @pytest.mark.asyncio
    async def test_alist_empty_prefix(self, store) -> None:
        """Test listing all files with empty prefix."""
        # Create some files
        for i in range(3):
            await store.aput(
                FileMetadata(
                    path=f"/listtest/file{i}.txt",
                    backend_name="local",
                    physical_path=f"/storage/list{i}",
                    size=100 * i,
                )
            )

        results = await store.alist(prefix="/listtest/")
        assert len(results) == 3
        paths = [r.path for r in results]
        assert "/listtest/file0.txt" in paths
        assert "/listtest/file1.txt" in paths
        assert "/listtest/file2.txt" in paths

    @pytest.mark.asyncio
    async def test_alist_with_prefix(self, store) -> None:
        """Test listing files under a specific prefix."""
        await store.aput(
            FileMetadata(
                path="/a/file.txt",
                backend_name="local",
                physical_path="/storage/a",
                size=100,
            )
        )
        await store.aput(
            FileMetadata(
                path="/b/file.txt",
                backend_name="local",
                physical_path="/storage/b",
                size=100,
            )
        )

        results = await store.alist(prefix="/a/")
        assert len(results) == 1
        assert results[0].path == "/a/file.txt"

    @pytest.mark.asyncio
    async def test_alist_recursive_false(self, store) -> None:
        """Test non-recursive listing returns only direct children."""
        # Create nested structure
        await store.aput(
            FileMetadata(
                path="/parent/direct.txt",
                backend_name="local",
                physical_path="/storage/direct",
                size=100,
            )
        )
        await store.aput(
            FileMetadata(
                path="/parent/sub/nested.txt",
                backend_name="local",
                physical_path="/storage/nested",
                size=100,
            )
        )

        # Non-recursive should only return direct.txt
        results = await store.alist(prefix="/parent/", recursive=False)
        assert len(results) == 1
        assert results[0].path == "/parent/direct.txt"

    @pytest.mark.asyncio
    async def test_alist_with_tenant_filter(self, store) -> None:
        """Test listing with tenant_id filter (Issue #904 PREWHERE optimization)."""
        # Create files for different tenants
        await store.aput(
            FileMetadata(
                path="/multi/acme.txt",
                backend_name="local",
                physical_path="/storage/acme",
                size=100,
                tenant_id="org_acme",
            )
        )
        await store.aput(
            FileMetadata(
                path="/multi/beta.txt",
                backend_name="local",
                physical_path="/storage/beta",
                size=100,
                tenant_id="org_beta",
            )
        )

        # Filter by tenant
        results = await store.alist(prefix="/multi/", tenant_id="org_acme")
        assert len(results) == 1
        assert results[0].path == "/multi/acme.txt"

    @pytest.mark.asyncio
    async def test_alist_excludes_deleted_files(self, store) -> None:
        """Test that deleted files are not included in listings."""
        await store.aput(
            FileMetadata(
                path="/dellist/keep.txt",
                backend_name="local",
                physical_path="/storage/keep",
                size=100,
            )
        )
        await store.aput(
            FileMetadata(
                path="/dellist/delete.txt",
                backend_name="local",
                physical_path="/storage/delete",
                size=100,
            )
        )

        # Delete one file
        await store.adelete("/dellist/delete.txt")

        # Should only return the kept file
        results = await store.alist(prefix="/dellist/")
        assert len(results) == 1
        assert results[0].path == "/dellist/keep.txt"


class TestAsyncExists:
    """Tests for async aexists() method."""

    @pytest.mark.asyncio
    async def test_aexists_true(self, store) -> None:
        """Test exists returns True for existing file."""
        await store.aput(
            FileMetadata(
                path="/exists/yes.txt",
                backend_name="local",
                physical_path="/storage/yes",
                size=100,
            )
        )

        result = await store.aexists("/exists/yes.txt")
        assert result is True

    @pytest.mark.asyncio
    async def test_aexists_false(self, store) -> None:
        """Test exists returns False for nonexistent file."""
        result = await store.aexists("/exists/no.txt")
        assert result is False

    @pytest.mark.asyncio
    async def test_aexists_deleted_returns_false(self, store) -> None:
        """Test exists returns False for soft-deleted file."""
        await store.aput(
            FileMetadata(
                path="/exists/deleted.txt",
                backend_name="local",
                physical_path="/storage/del",
                size=100,
            )
        )
        await store.adelete("/exists/deleted.txt")

        result = await store.aexists("/exists/deleted.txt")
        assert result is False


class TestAsyncListPaginated:
    """Tests for async alist_paginated() method."""

    @pytest.mark.asyncio
    async def test_alist_paginated_first_page(self, store) -> None:
        """Test getting first page of results."""
        # Create 10 files
        for i in range(10):
            await store.aput(
                FileMetadata(
                    path=f"/paginated/file{i:02d}.txt",
                    backend_name="local",
                    physical_path=f"/storage/pg{i}",
                    size=100,
                )
            )

        result = await store.alist_paginated(prefix="/paginated/", limit=5)
        assert len(result.items) == 5
        assert result.has_more is True
        assert result.next_cursor is not None

    @pytest.mark.asyncio
    async def test_alist_paginated_with_cursor(self, store) -> None:
        """Test paginating through results with cursor."""
        # Create 10 files
        for i in range(10):
            await store.aput(
                FileMetadata(
                    path=f"/cursor/file{i:02d}.txt",
                    backend_name="local",
                    physical_path=f"/storage/cur{i}",
                    size=100,
                )
            )

        # Get first page
        page1 = await store.alist_paginated(prefix="/cursor/", limit=5)
        assert len(page1.items) == 5

        # Get second page using cursor
        page2 = await store.alist_paginated(prefix="/cursor/", limit=5, cursor=page1.next_cursor)
        assert len(page2.items) == 5

        # Verify no overlap
        page1_paths = {item.path for item in page1.items}
        page2_paths = {item.path for item in page2.items}
        assert page1_paths.isdisjoint(page2_paths)

    @pytest.mark.asyncio
    async def test_alist_paginated_last_page(self, store) -> None:
        """Test that last page has has_more=False."""
        # Create 3 files
        for i in range(3):
            await store.aput(
                FileMetadata(
                    path=f"/lastpage/file{i}.txt",
                    backend_name="local",
                    physical_path=f"/storage/lp{i}",
                    size=100,
                )
            )

        result = await store.alist_paginated(prefix="/lastpage/", limit=10)
        assert len(result.items) == 3
        assert result.has_more is False


class TestConcurrency:
    """Tests for concurrent async operations."""

    @pytest.mark.asyncio
    async def test_concurrent_reads(self, store) -> None:
        """Test multiple concurrent aget calls."""
        # Create a file
        await store.aput(
            FileMetadata(
                path="/concurrent/read.txt",
                backend_name="local",
                physical_path="/storage/conc",
                size=100,
            )
        )

        # Run 10 concurrent reads
        tasks = [store.aget("/concurrent/read.txt") for _ in range(10)]
        results = await asyncio.gather(*tasks)

        # All should succeed
        assert all(r is not None for r in results)
        assert all(r.path == "/concurrent/read.txt" for r in results)

    @pytest.mark.asyncio
    async def test_concurrent_writes(self, store) -> None:
        """Test multiple concurrent aput calls to different files."""
        # Create 10 files concurrently
        tasks = [
            store.aput(
                FileMetadata(
                    path=f"/concurrent/write{i}.txt",
                    backend_name="local",
                    physical_path=f"/storage/cw{i}",
                    size=100 + i,
                )
            )
            for i in range(10)
        ]
        await asyncio.gather(*tasks)

        # Verify all files were created
        results = await store.alist(prefix="/concurrent/write")
        assert len(results) == 10

    @pytest.mark.asyncio
    async def test_concurrent_list_during_writes(self, store) -> None:
        """Test listing while writes are happening."""
        # Create initial files
        for i in range(5):
            await store.aput(
                FileMetadata(
                    path=f"/mixed/file{i}.txt",
                    backend_name="local",
                    physical_path=f"/storage/m{i}",
                    size=100,
                )
            )

        # Run concurrent list and writes
        async def write_file(idx: int):
            await store.aput(
                FileMetadata(
                    path=f"/mixed/new{idx}.txt",
                    backend_name="local",
                    physical_path=f"/storage/new{idx}",
                    size=200,
                )
            )

        async def list_files():
            return await store.alist(prefix="/mixed/")

        # Mixed operations
        tasks = [
            list_files(),
            write_file(1),
            list_files(),
            write_file(2),
        ]
        results = await asyncio.gather(*tasks)

        # Lists should return results (exact count depends on timing)
        list_results = [r for r in results if isinstance(r, list)]
        assert all(len(r) >= 5 for r in list_results)


class TestErrorHandling:
    """Tests for error handling in async operations."""

    @pytest.mark.asyncio
    async def test_aput_invalid_path(self, store) -> None:
        """Test that invalid paths are rejected."""
        from nexus.core.exceptions import ValidationError

        # Path without leading /
        with pytest.raises(ValidationError):
            await store.aput(
                FileMetadata(
                    path="invalid/path.txt",  # Missing leading /
                    backend_name="local",
                    physical_path="/storage/inv",
                    size=100,
                )
            )

    @pytest.mark.asyncio
    async def test_aput_negative_size(self, store) -> None:
        """Test that negative size is rejected."""
        from nexus.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            await store.aput(
                FileMetadata(
                    path="/invalid/negative.txt",
                    backend_name="local",
                    physical_path="/storage/neg",
                    size=-100,  # Invalid
                )
            )


class TestCacheInvalidation:
    """Tests for cache invalidation behavior."""

    @pytest.mark.asyncio
    async def test_aput_invalidates_list_cache(self, store) -> None:
        """Test that put invalidates list cache for parent directories."""
        # Create initial file
        await store.aput(
            FileMetadata(
                path="/cacheinv/existing.txt",
                backend_name="local",
                physical_path="/storage/ex",
                size=100,
            )
        )

        # List to populate cache
        results1 = await store.alist(prefix="/cacheinv/")
        assert len(results1) == 1

        # Add new file
        await store.aput(
            FileMetadata(
                path="/cacheinv/new.txt",
                backend_name="local",
                physical_path="/storage/new",
                size=200,
            )
        )

        # List again - should see new file (cache invalidated)
        results2 = await store.alist(prefix="/cacheinv/")
        assert len(results2) == 2

    @pytest.mark.asyncio
    async def test_adelete_invalidates_list_cache(self, store) -> None:
        """Test that delete invalidates list cache."""
        # Create files
        await store.aput(
            FileMetadata(
                path="/delinv/keep.txt",
                backend_name="local",
                physical_path="/storage/k",
                size=100,
            )
        )
        await store.aput(
            FileMetadata(
                path="/delinv/remove.txt",
                backend_name="local",
                physical_path="/storage/r",
                size=100,
            )
        )

        # List to populate cache
        results1 = await store.alist(prefix="/delinv/")
        assert len(results1) == 2

        # Delete one file
        await store.adelete("/delinv/remove.txt")

        # List again - should reflect deletion
        results2 = await store.alist(prefix="/delinv/")
        assert len(results2) == 1
