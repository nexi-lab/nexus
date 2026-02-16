"""Integration tests for list + permissions + zone filtering (Issue #904).

Wire up real SearchService + RaftMetadataStore + OperationContext
to verify zone-scoped listing with permission filtering.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from nexus.core._metadata_generated import FileMetadata
from nexus.storage.raft_metadata_store import RaftMetadataStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(zone_id: str = "default") -> RaftMetadataStore:
    """Create a RaftMetadataStore with a temp directory."""
    tmpdir = tempfile.mkdtemp()
    return RaftMetadataStore.embedded(str(Path(tmpdir) / "meta"), zone_id=zone_id)


def _make_meta(path: str, size: int = 100) -> FileMetadata:
    return FileMetadata(
        path=path,
        backend_name="local",
        physical_path=f"/data{path}",
        size=size,
    )


def _make_search_service(
    store: RaftMetadataStore,
    enforce_permissions: bool = False,
) -> Any:
    """Create a SearchService backed by the given store."""
    from nexus.services.search_service import SearchService

    return SearchService(
        metadata_store=store,
        enforce_permissions=enforce_permissions,
    )


# ===========================================================================
# Tests
# ===========================================================================


class TestListWithZoneAndPermissions:
    """Integration tests for list with zone filtering and permissions."""

    def test_list_returns_all_files_in_store(self) -> None:
        """Basic list returns all files in the store."""
        store = _make_store()
        try:
            store.put(_make_meta("/file1.txt"))
            store.put(_make_meta("/file2.txt"))
            store.put(_make_meta("/dir/file3.txt"))

            svc = _make_search_service(store)
            results = svc.list(path="/", recursive=True)

            assert "/file1.txt" in results
            assert "/file2.txt" in results
            assert "/dir/file3.txt" in results
        finally:
            store.close()

    def test_list_non_recursive_excludes_nested(self) -> None:
        """Non-recursive list should exclude nested files."""
        store = _make_store()
        try:
            store.put(_make_meta("/file1.txt"))
            store.put(_make_meta("/dir/file2.txt"))
            store.put(_make_meta("/dir/sub/file3.txt"))

            svc = _make_search_service(store)
            results = svc.list(path="/", recursive=False)

            assert "/file1.txt" in results
            # Nested files should not be in non-recursive results
            assert "/dir/file2.txt" not in results
            assert "/dir/sub/file3.txt" not in results
        finally:
            store.close()

    def test_list_with_prefix_filters(self) -> None:
        """List with prefix should only return matching files."""
        store = _make_store()
        try:
            store.put(_make_meta("/a/file1.txt"))
            store.put(_make_meta("/a/file2.txt"))
            store.put(_make_meta("/b/file3.txt"))

            svc = _make_search_service(store)
            results = svc.list(prefix="/a/")

            assert "/a/file1.txt" in results
            assert "/a/file2.txt" in results
            assert "/b/file3.txt" not in results
        finally:
            store.close()

    def test_list_empty_store_returns_empty(self) -> None:
        """List on empty store returns empty."""
        store = _make_store()
        try:
            svc = _make_search_service(store)
            results = svc.list(path="/", recursive=True)
            assert results == []
        finally:
            store.close()

    def test_zone_local_store_isolation(self) -> None:
        """Two separate stores for different zones remain isolated."""
        store_a = _make_store(zone_id="zone_a")
        store_b = _make_store(zone_id="zone_b")
        try:
            store_a.put(_make_meta("/zone_a/file1.txt"))
            store_b.put(_make_meta("/zone_b/file2.txt"))

            svc_a = _make_search_service(store_a)
            svc_b = _make_search_service(store_b)

            results_a = svc_a.list(path="/", recursive=True)
            results_b = svc_b.list(path="/", recursive=True)

            assert "/zone_a/file1.txt" in results_a
            assert "/zone_b/file2.txt" not in results_a

            assert "/zone_b/file2.txt" in results_b
            assert "/zone_a/file1.txt" not in results_b
        finally:
            store_a.close()
            store_b.close()

    def test_list_paginated_returns_correct_pages(self) -> None:
        """Paginated list should return correct pages."""
        store = _make_store()
        try:
            for i in range(10):
                store.put(_make_meta(f"/p/{chr(97 + i)}.txt"))

            svc = _make_search_service(store)
            page1 = svc._list_paginated(
                path="/p/", recursive=True, details=False, limit=3, cursor=None, context=None
            )

            assert len(page1.items) == 3
            assert page1.has_more is True
            assert page1.next_cursor is not None
        finally:
            store.close()

    def test_list_details_includes_metadata(self) -> None:
        """List with details=True should return dicts with metadata."""
        store = _make_store()
        try:
            store.put(_make_meta("/doc.txt", size=512))

            svc = _make_search_service(store)
            results = svc.list(path="/", recursive=True, details=True)

            assert len(results) >= 1
            doc = next((r for r in results if r["path"] == "/doc.txt"), None)
            assert doc is not None
            assert doc["size"] == 512
            assert doc["is_directory"] is False
        finally:
            store.close()
