"""Tests for RaftMetadataStore serialization and pagination.

Tests the pure Python logic of _serialize_metadata, _deserialize_metadata,
and list_paginated without requiring the Rust PyO3 library.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from nexus.core._metadata_generated import FileMetadata, PaginatedResult
from nexus.core.consistency import StoreMode

if TYPE_CHECKING:
    from nexus.storage.raft_metadata_store import RaftMetadataStore


def _make_metadata(
    path: str = "/test/file.txt",
    backend_name: str = "local",
    physical_path: str = "/data/abc123",
    size: int = 1024,
    **kwargs: Any,
) -> FileMetadata:
    """Helper to create FileMetadata with sensible defaults."""
    return FileMetadata(
        path=path,
        backend_name=backend_name,
        physical_path=physical_path,
        size=size,
        **kwargs,
    )


class TestSerializeMetadata:
    """Tests for _serialize_metadata."""

    def test_json_roundtrip_basic(self) -> None:
        """Serialize and deserialize should produce identical metadata."""
        from nexus.storage.raft_metadata_store import (
            _deserialize_metadata,
            _serialize_metadata,
        )

        original = _make_metadata(
            path="/zone1/docs/readme.md",
            size=256,
            etag="abc123",
            mime_type="text/markdown",
            version=3,
            zone_id="zone1",
            is_directory=False,
        )
        data = _serialize_metadata(original)
        restored = _deserialize_metadata(data)

        assert restored.path == original.path
        assert restored.backend_name == original.backend_name
        assert restored.size == original.size
        assert restored.etag == original.etag
        assert restored.mime_type == original.mime_type
        assert restored.version == original.version
        assert restored.zone_id == original.zone_id
        assert restored.is_directory == original.is_directory

    def test_json_roundtrip_with_timestamps(self) -> None:
        """Timestamps should survive serialization round-trip."""
        from nexus.storage.raft_metadata_store import (
            _deserialize_metadata,
            _serialize_metadata,
        )

        now = datetime(2026, 2, 10, 12, 0, 0)
        original = _make_metadata(created_at=now, modified_at=now)
        data = _serialize_metadata(original)
        restored = _deserialize_metadata(data)

        assert restored.created_at == now
        assert restored.modified_at == now

    def test_json_roundtrip_optional_none_fields(self) -> None:
        """None optional fields should round-trip correctly."""
        from nexus.storage.raft_metadata_store import (
            _deserialize_metadata,
            _serialize_metadata,
        )

        original = _make_metadata(
            etag=None,
            mime_type=None,
            created_at=None,
            modified_at=None,
            zone_id=None,
            created_by=None,
            owner_id=None,
        )
        data = _serialize_metadata(original)
        restored = _deserialize_metadata(data)

        assert restored.etag is None
        assert restored.mime_type is None
        assert restored.created_at is None
        assert restored.modified_at is None
        assert restored.zone_id is None

    def test_json_roundtrip_directory(self) -> None:
        """Directory metadata should round-trip correctly."""
        from nexus.storage.raft_metadata_store import (
            _deserialize_metadata,
            _serialize_metadata,
        )

        original = _make_metadata(
            path="/zone1/docs/",
            is_directory=True,
            size=0,
        )
        data = _serialize_metadata(original)
        restored = _deserialize_metadata(data)

        assert restored.is_directory is True
        assert restored.path == "/zone1/docs/"

    def test_deserialize_list_of_ints(self) -> None:
        """PyO3 may return list[int] instead of bytes; deserialize should handle it."""
        from nexus.storage.raft_metadata_store import (
            _deserialize_metadata,
            _serialize_metadata,
        )

        original = _make_metadata()
        data = _serialize_metadata(original)
        # Convert bytes to list of ints (simulating PyO3 behavior)
        data_as_list = list(data)
        restored = _deserialize_metadata(data_as_list)

        assert restored.path == original.path
        assert restored.size == original.size

    def test_deserialize_invalid_data_raises(self) -> None:
        """Invalid data should raise ValueError, not silently fail."""
        from nexus.storage.raft_metadata_store import _deserialize_metadata

        with pytest.raises(ValueError, match="Failed to deserialize"):
            _deserialize_metadata(b"not valid json or protobuf")

    def test_deserialize_empty_bytes_raises(self) -> None:
        """Empty bytes should raise ValueError."""
        from nexus.storage.raft_metadata_store import _deserialize_metadata

        with pytest.raises(ValueError):
            _deserialize_metadata(b"")


class TestListPaginated:
    """Tests for RaftMetadataStore.list_paginated."""

    def _make_store_with_entries(
        self, entries: list[tuple[str, FileMetadata]]
    ) -> RaftMetadataStore:
        """Create a mock RaftMetadataStore with pre-loaded entries."""
        from nexus.storage.raft_metadata_store import (
            RaftMetadataStore,
            _serialize_metadata,
        )

        # Build the mock LocalRaft
        mock_local = MagicMock()

        # list_metadata returns (path, serialized_bytes) tuples
        def list_metadata_side_effect(prefix: str) -> list[tuple[str, bytes]]:
            return [
                (path, _serialize_metadata(meta))
                for path, meta in entries
                if path.startswith(prefix)
            ]

        mock_local.list_metadata.side_effect = list_metadata_side_effect

        # Create store via __new__ to bypass __init__
        store = object.__new__(RaftMetadataStore)
        store._mode = StoreMode.EMBEDDED
        store._local = mock_local
        store._remote = None
        store._zone_id = None
        return store

    def test_paginated_basic(self) -> None:
        """Basic pagination returns first page."""
        entries = [(f"/files/f{i}.txt", _make_metadata(path=f"/files/f{i}.txt")) for i in range(5)]
        store = self._make_store_with_entries(entries)

        result = store.list_paginated(prefix="/files/", limit=3)

        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 3
        assert result.has_more is True
        assert result.total_count == 5
        assert result.next_cursor is not None

    def test_paginated_all_fit_in_one_page(self) -> None:
        """When all items fit in one page, has_more should be False."""
        entries = [(f"/files/f{i}.txt", _make_metadata(path=f"/files/f{i}.txt")) for i in range(3)]
        store = self._make_store_with_entries(entries)

        result = store.list_paginated(prefix="/files/", limit=10)

        assert len(result.items) == 3
        assert result.has_more is False
        assert result.next_cursor is None
        assert result.total_count == 3

    def test_paginated_empty_results(self) -> None:
        """Empty prefix returns empty results."""
        store = self._make_store_with_entries([])

        result = store.list_paginated(prefix="/nonexistent/", limit=10)

        assert len(result.items) == 0
        assert result.has_more is False
        assert result.total_count == 0

    def test_paginated_cursor_pagination(self) -> None:
        """Cursor-based pagination navigates through pages."""
        entries = [
            (f"/files/{chr(97 + i)}.txt", _make_metadata(path=f"/files/{chr(97 + i)}.txt"))
            for i in range(5)  # a.txt, b.txt, c.txt, d.txt, e.txt
        ]
        store = self._make_store_with_entries(entries)

        # First page
        page1 = store.list_paginated(prefix="/files/", limit=2)
        assert len(page1.items) == 2
        assert page1.has_more is True
        assert page1.items[0].path == "/files/a.txt"
        assert page1.items[1].path == "/files/b.txt"

        # Second page using cursor
        page2 = store.list_paginated(prefix="/files/", limit=2, cursor=page1.next_cursor)
        assert len(page2.items) == 2
        assert page2.has_more is True
        assert page2.items[0].path == "/files/c.txt"
        assert page2.items[1].path == "/files/d.txt"

        # Third page — last page
        page3 = store.list_paginated(prefix="/files/", limit=2, cursor=page2.next_cursor)
        assert len(page3.items) == 1
        assert page3.has_more is False
        assert page3.items[0].path == "/files/e.txt"

    def test_paginated_non_recursive_filters_nested(self) -> None:
        """Non-recursive listing should exclude nested paths."""
        entries = [
            ("/root/a.txt", _make_metadata(path="/root/a.txt")),
            ("/root/b.txt", _make_metadata(path="/root/b.txt")),
            ("/root/sub/c.txt", _make_metadata(path="/root/sub/c.txt")),
            ("/root/sub/deep/d.txt", _make_metadata(path="/root/sub/deep/d.txt")),
        ]
        store = self._make_store_with_entries(entries)

        result = store.list_paginated(prefix="/root/", recursive=False, limit=10)

        # Only direct children of /root/ should be returned
        paths = [item.path for item in result.items]
        assert "/root/a.txt" in paths
        assert "/root/b.txt" in paths
        assert "/root/sub/c.txt" not in paths
        assert "/root/sub/deep/d.txt" not in paths

    def test_paginated_skips_meta_keys(self) -> None:
        """Extended attribute keys (meta:...) should be skipped."""
        from nexus.storage.raft_metadata_store import _serialize_metadata

        mock_local = MagicMock()
        mock_local.list_metadata.return_value = [
            ("/files/a.txt", _serialize_metadata(_make_metadata(path="/files/a.txt"))),
            (
                "meta:/files/a.txt:custom_attr",
                _serialize_metadata(_make_metadata(path="meta:/files/a.txt:custom_attr")),
            ),
        ]

        from nexus.storage.raft_metadata_store import RaftMetadataStore

        store = object.__new__(RaftMetadataStore)
        store._mode = StoreMode.EMBEDDED
        store._local = mock_local
        store._remote = None
        store._zone_id = None

        result = store.list_paginated(prefix="/files/", limit=10)

        assert len(result.items) == 1
        assert result.items[0].path == "/files/a.txt"
