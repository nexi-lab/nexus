"""Integration tests for list() pagination at scale (Issue #937).

These tests verify end-to-end pagination behavior including:
- Permission filtering with pagination
- API endpoint responses
- Large dataset handling
- Backward compatibility
"""

import pytest

from nexus.backends.storage.cas_local import CASLocalBackend
from nexus.core.config import PermissionConfig
from nexus.core.pagination import PaginatedResult
from nexus.factory import create_nexus_fs
from nexus.storage.raft_metadata_store import RaftMetadataStore


@pytest.fixture
async def nexus_fs(tmp_path, isolated_db):
    """Create a NexusFS instance for testing via factory."""
    backend = CASLocalBackend(str(tmp_path / "data"))
    metadata_store = RaftMetadataStore.embedded(str(isolated_db).replace(".db", ""))
    nx = await create_nexus_fs(
        backend=backend, metadata_store=metadata_store, permissions=PermissionConfig(enforce=False)
    )
    yield nx
    nx.close()


@pytest.fixture
async def nexus_fs_with_files(nexus_fs):
    """Create NexusFS with 100 test files."""
    for i in range(100):
        nexus_fs.write(f"/workspace/file{i:03d}.txt", f"content {i}")
    return nexus_fs


@pytest.fixture
async def nexus_fs_large(tmp_path, isolated_db):
    """Create NexusFS with 1000 test files for scale testing."""
    backend = CASLocalBackend(str(tmp_path / "data"))
    metadata_store = RaftMetadataStore.embedded(str(isolated_db).replace(".db", ""))
    nx = await create_nexus_fs(
        backend=backend,
        metadata_store=metadata_store,
        permissions=PermissionConfig(enforce=False),
        enable_write_buffer=False,  # sync observer — 1000-file burst can overflow pipe buffer
    )

    # Create 1000 files in batches
    for i in range(1000):
        nx.write(f"/large/file{i:04d}.txt", f"content {i}")

    yield nx
    nx.close()


class TestPaginatedListBasic:
    """Basic pagination tests."""

    @pytest.mark.asyncio
    async def test_paginated_list_returns_paginated_result(self, nexus_fs_with_files):
        """list() with limit should return PaginatedResult."""
        result = nexus_fs_with_files.sys_readdir(
            path="/workspace/",
            limit=10,
        )

        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 10
        assert result.has_more is True
        assert result.next_cursor is not None

    @pytest.mark.asyncio
    async def test_paginated_list_path_only_mode(self, nexus_fs_with_files):
        """Paginated list with details=False should return paths."""
        result = nexus_fs_with_files.sys_readdir(
            path="/workspace/",
            limit=10,
            details=False,
        )

        assert isinstance(result, PaginatedResult)
        assert all(isinstance(item, str) for item in result.items)
        assert result.items[0].startswith("/workspace/")

    @pytest.mark.asyncio
    async def test_paginated_list_details_mode(self, nexus_fs_with_files):
        """Paginated list with details=True should return dicts."""
        result = nexus_fs_with_files.sys_readdir(
            path="/workspace/",
            limit=10,
            details=True,
        )

        assert isinstance(result, PaginatedResult)
        assert all(isinstance(item, dict) for item in result.items)
        assert "path" in result.items[0]
        assert "size" in result.items[0]

    @pytest.mark.asyncio
    async def test_iterate_through_all_pages(self, nexus_fs_with_files):
        """Should iterate through all files without duplicates."""
        all_paths = []
        cursor = None
        page_count = 0

        while True:
            result = nexus_fs_with_files.sys_readdir(
                path="/workspace/",
                limit=15,
                cursor=cursor,
            )

            all_paths.extend(result.items)
            page_count += 1

            if not result.has_more:
                break
            cursor = result.next_cursor

            # Safety
            assert page_count < 20

        assert len(all_paths) == 100
        assert len(set(all_paths)) == 100  # No duplicates

    @pytest.mark.asyncio
    async def test_last_page_has_no_cursor(self, nexus_fs_with_files):
        """Last page should have next_cursor=None and has_more=False."""
        result = nexus_fs_with_files.sys_readdir(
            path="/workspace/",
            limit=200,  # More than total files
        )

        assert result.has_more is False
        assert result.next_cursor is None


class TestBackwardCompatibility:
    """Tests for backward compatibility."""

    @pytest.mark.asyncio
    async def test_list_without_limit_returns_list(self, nexus_fs_with_files):
        """list() without limit should return regular list."""
        result = nexus_fs_with_files.sys_readdir(path="/workspace/")

        # Should be a regular list, not PaginatedResult
        assert isinstance(result, list)
        assert not isinstance(result, PaginatedResult)
        assert len(result) == 100

    @pytest.mark.asyncio
    async def test_list_without_limit_details_mode(self, nexus_fs_with_files):
        """list() without limit and details=True should return list of dicts."""
        result = nexus_fs_with_files.sys_readdir(path="/workspace/", details=True)

        assert isinstance(result, list)
        assert all(isinstance(item, dict) for item in result)

    @pytest.mark.asyncio
    async def test_existing_tests_still_pass(self, nexus_fs):
        """Existing list() behavior should be unchanged."""
        # Create directories and files
        nexus_fs.mkdir("/test/sub", exist_ok=True, parents=True)
        nexus_fs.write("/test/a.txt", "a")
        nexus_fs.write("/test/b.txt", "b")
        nexus_fs.write("/test/sub/c.txt", "c")

        # Recursive list (default) — returns files and dirs
        result = nexus_fs.sys_readdir("/test/")
        assert len(result) >= 3  # at least a.txt, b.txt, sub/c.txt

        # Non-recursive list — direct children only (may include sub/ dir entry)
        result = nexus_fs.sys_readdir("/test/", recursive=False)
        assert len(result) >= 2  # at least a.txt, b.txt


class TestPaginationAtScale:
    """Tests for pagination with larger datasets."""

    @pytest.mark.asyncio
    async def test_paginate_1000_files(self, nexus_fs_large):
        """Should paginate through 1000 files correctly."""
        all_items = []
        cursor = None
        page_count = 0

        while True:
            result = nexus_fs_large.sys_readdir(
                path="/large/",
                limit=100,
                cursor=cursor,
            )

            all_items.extend(result.items)
            page_count += 1

            if not result.has_more:
                break
            cursor = result.next_cursor

        assert len(all_items) == 1000
        assert len(set(all_items)) == 1000
        assert page_count == 10  # 1000 files / 100 per page

    @pytest.mark.asyncio
    async def test_small_pages(self, nexus_fs_large):
        """Should work with very small page sizes."""
        result = nexus_fs_large.sys_readdir(path="/large/", limit=1)

        assert len(result.items) == 1
        assert result.has_more is True

    @pytest.mark.asyncio
    async def test_large_single_page(self, nexus_fs_large):
        """Should handle large single page requests."""
        result = nexus_fs_large.sys_readdir(path="/large/", limit=10000)

        assert len(result.items) == 1000
        assert result.has_more is False


class TestPaginationWithPermissions:
    """Tests for pagination with permission filtering.

    Note: Full permission filtering tests are complex and require
    proper ReBAC setup. Basic permission context is tested here.
    """

    @pytest.mark.asyncio
    async def test_pagination_without_permissions(self, nexus_fs_with_files):
        """Pagination should work when permissions are disabled."""
        # nexus_fs fixture has enforce_permissions=False
        result = nexus_fs_with_files.sys_readdir(
            path="/workspace/",
            limit=10,
        )

        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 10
        assert result.has_more is True


class TestPaginationEdgeCases:
    """Tests for edge cases in pagination."""

    @pytest.mark.asyncio
    async def test_empty_directory(self, nexus_fs):
        """Should handle empty directories."""
        result = nexus_fs.sys_readdir(path="/empty/", limit=10)

        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 0
        assert result.has_more is False
        assert result.next_cursor is None

    @pytest.mark.asyncio
    async def test_non_recursive_pagination(self, nexus_fs):
        """Should work with recursive=False."""
        # Create nested structure
        nexus_fs.write("/dir/file1.txt", "1")
        nexus_fs.write("/dir/file2.txt", "2")
        nexus_fs.write("/dir/sub/file3.txt", "3")
        nexus_fs.write("/dir/sub/deep/file4.txt", "4")

        result = nexus_fs.sys_readdir(
            path="/dir/",
            recursive=False,
            limit=10,
        )

        # Should only get direct children
        paths = result.items
        # Note: directories may also be included
        assert any("file1.txt" in str(p) for p in paths)
        assert any("file2.txt" in str(p) for p in paths)
        # Deep files should not be included directly
        assert not any("file4.txt" in str(p) for p in paths)

    @pytest.mark.asyncio
    async def test_pagination_with_special_characters(self, nexus_fs):
        """Should handle paths with special characters."""
        nexus_fs.write("/test/file with spaces.txt", "content")
        nexus_fs.write("/test/file-with-dashes.txt", "content")
        nexus_fs.write("/test/file_with_underscores.txt", "content")

        result = nexus_fs.sys_readdir(path="/test/", limit=10)

        assert len(result.items) == 3

    @pytest.mark.asyncio
    async def test_cursor_from_deleted_position(self, nexus_fs):
        """Should handle cursor pointing to deleted file gracefully."""
        # Create files
        for i in range(20):
            nexus_fs.write(f"/test/file{i:02d}.txt", f"content {i}")

        # Get first page
        page1 = nexus_fs.sys_readdir(path="/test/", limit=10)

        # Delete some files (simulating concurrent modification)
        nexus_fs.sys_unlink("/test/file09.txt")
        nexus_fs.sys_unlink("/test/file10.txt")

        # Continue pagination - should work even with deleted files
        page2 = nexus_fs.sys_readdir(
            path="/test/",
            limit=10,
            cursor=page1.next_cursor,
        )

        # Should still return results
        assert isinstance(page2, PaginatedResult)
