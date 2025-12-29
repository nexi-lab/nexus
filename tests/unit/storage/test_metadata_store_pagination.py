"""Unit tests for metadata store pagination (Issue #937)."""

import tempfile
from pathlib import Path

import pytest

from nexus.core.metadata import FileMetadata, PaginatedResult
from nexus.core.pagination import CursorError
from nexus.storage.metadata_store import SQLAlchemyMetadataStore


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    # Cleanup
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def store(temp_db):
    """Create a temporary metadata store."""
    store = SQLAlchemyMetadataStore(temp_db)
    yield store
    store.close()


@pytest.fixture
def store_with_files(store):
    """Create store with 100 test files."""
    for i in range(100):
        store.put(
            FileMetadata(
                path=f"/files/file{i:03d}.txt",
                backend_name="local",
                physical_path=f"/data/file{i:03d}.txt",
                size=100 + i,
                tenant_id="test_tenant",
            )
        )
    return store


class TestListPaginated:
    """Tests for list_paginated method."""

    def test_first_page(self, store_with_files):
        """Should return first N items with next_cursor."""
        result = store_with_files.list_paginated(
            prefix="/files/",
            limit=10,
        )

        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 10
        assert result.has_more is True
        assert result.next_cursor is not None
        assert result.items[0].path == "/files/file000.txt"
        assert result.items[9].path == "/files/file009.txt"

    def test_pagination_continues(self, store_with_files):
        """Should continue from cursor correctly."""
        page1 = store_with_files.list_paginated(prefix="/files/", limit=10)
        page2 = store_with_files.list_paginated(
            prefix="/files/",
            limit=10,
            cursor=page1.next_cursor,
        )

        assert page2.items[0].path == "/files/file010.txt"
        assert len(page2.items) == 10

    def test_last_page_no_cursor(self, store_with_files):
        """Last page should have no next_cursor."""
        result = store_with_files.list_paginated(prefix="/files/", limit=100)

        assert len(result.items) == 100
        assert result.has_more is False
        assert result.next_cursor is None

    def test_iterate_all_pages(self, store_with_files):
        """Should iterate through all items without duplicates or gaps."""
        all_paths = []
        cursor = None
        page_count = 0

        while True:
            result = store_with_files.list_paginated(
                prefix="/files/",
                limit=15,
                cursor=cursor,
            )
            all_paths.extend(item.path for item in result.items)
            page_count += 1

            if not result.has_more:
                break
            cursor = result.next_cursor

            # Safety: prevent infinite loop in tests
            assert page_count < 20, "Too many pages"

        assert len(all_paths) == 100
        assert len(set(all_paths)) == 100  # No duplicates
        # Verify order
        assert all_paths == sorted(all_paths)

    def test_limit_of_one(self, store_with_files):
        """Should work with limit=1."""
        result = store_with_files.list_paginated(prefix="/files/", limit=1)

        assert len(result.items) == 1
        assert result.has_more is True
        assert result.items[0].path == "/files/file000.txt"

    def test_limit_larger_than_dataset(self, store_with_files):
        """Should return all items when limit > total files."""
        result = store_with_files.list_paginated(prefix="/files/", limit=500)

        assert len(result.items) == 100
        assert result.has_more is False
        assert result.next_cursor is None

    def test_limit_capped_at_max(self, store_with_files):
        """Limit should be capped at 10000."""
        result = store_with_files.list_paginated(prefix="/files/", limit=999999)

        # Should not crash, limit internally capped
        assert len(result.items) == 100  # Only 100 files exist

    def test_limit_minimum_is_one(self, store_with_files):
        """Limit should have minimum of 1."""
        result = store_with_files.list_paginated(prefix="/files/", limit=0)

        # Should return at least 1 item (limit capped at minimum)
        assert len(result.items) >= 1

    def test_empty_result(self, store):
        """Should handle empty results gracefully."""
        result = store.list_paginated(prefix="/nonexistent/", limit=10)

        assert len(result.items) == 0
        assert result.has_more is False
        assert result.next_cursor is None

    def test_tenant_filtering(self, store):
        """Should filter by tenant_id."""
        # Create files for different tenants
        for i in range(10):
            store.put(
                FileMetadata(
                    path=f"/files/tenant1_file{i}.txt",
                    backend_name="local",
                    physical_path=f"/data/file{i}.txt",
                    size=100,
                    tenant_id="tenant1",
                )
            )
        for i in range(5):
            store.put(
                FileMetadata(
                    path=f"/files/tenant2_file{i}.txt",
                    backend_name="local",
                    physical_path=f"/data/file{i}.txt",
                    size=100,
                    tenant_id="tenant2",
                )
            )

        result = store.list_paginated(
            prefix="/files/",
            limit=100,
            tenant_id="tenant1",
        )

        # Should return only tenant1 files
        assert len(result.items) == 10
        assert all("tenant1" in item.path for item in result.items)

    def test_non_recursive(self, store):
        """Should respect recursive=False."""
        store.put(
            FileMetadata(
                path="/a/file1.txt", backend_name="local", physical_path="/x", size=1
            )
        )
        store.put(
            FileMetadata(
                path="/a/b/file2.txt", backend_name="local", physical_path="/x", size=1
            )
        )
        store.put(
            FileMetadata(
                path="/a/b/c/file3.txt",
                backend_name="local",
                physical_path="/x",
                size=1,
            )
        )

        result = store.list_paginated(prefix="/a/", recursive=False, limit=10)

        assert len(result.items) == 1
        assert result.items[0].path == "/a/file1.txt"

    def test_recursive_true(self, store):
        """Should include nested files with recursive=True."""
        store.put(
            FileMetadata(
                path="/a/file1.txt", backend_name="local", physical_path="/x", size=1
            )
        )
        store.put(
            FileMetadata(
                path="/a/b/file2.txt", backend_name="local", physical_path="/x", size=1
            )
        )
        store.put(
            FileMetadata(
                path="/a/b/c/file3.txt",
                backend_name="local",
                physical_path="/x",
                size=1,
            )
        )

        result = store.list_paginated(prefix="/a/", recursive=True, limit=10)

        assert len(result.items) == 3

    def test_cursor_invalid_format(self, store_with_files):
        """Should raise CursorError for invalid cursor."""
        with pytest.raises(CursorError):
            store_with_files.list_paginated(
                prefix="/files/",
                limit=10,
                cursor="invalid-cursor",
            )

    def test_cursor_filter_mismatch(self, store_with_files):
        """Should raise CursorError if filters changed between pages."""
        # Get cursor with one prefix
        page1 = store_with_files.list_paginated(prefix="/files/", limit=10)

        # Try to use cursor with different prefix
        with pytest.raises(CursorError, match="filters mismatch"):
            store_with_files.list_paginated(
                prefix="/other/",  # Different prefix!
                limit=10,
                cursor=page1.next_cursor,
            )

    def test_results_ordered_by_path(self, store):
        """Results should be ordered by path."""
        # Insert in random order
        paths = ["/z/file.txt", "/a/file.txt", "/m/file.txt", "/b/file.txt"]
        for path in paths:
            store.put(
                FileMetadata(
                    path=path, backend_name="local", physical_path="/x", size=1
                )
            )

        result = store.list_paginated(prefix="/", limit=10)

        result_paths = [item.path for item in result.items]
        assert result_paths == sorted(result_paths)


class TestPaginatedResult:
    """Tests for PaginatedResult dataclass."""

    def test_to_dict(self):
        """to_dict should convert to JSON-serializable format."""
        result = PaginatedResult(
            items=["/file1.txt", "/file2.txt"],
            next_cursor="abc123",
            has_more=True,
            total_count=100,
        )

        d = result.to_dict()

        assert d["items"] == ["/file1.txt", "/file2.txt"]
        assert d["next_cursor"] == "abc123"
        assert d["has_more"] is True
        assert d["total_count"] == 100

    def test_to_dict_no_cursor(self):
        """to_dict should handle None cursor."""
        result = PaginatedResult(
            items=[],
            next_cursor=None,
            has_more=False,
            total_count=0,
        )

        d = result.to_dict()

        assert d["next_cursor"] is None
        assert d["has_more"] is False
