"""Tests for RaftMetadataStore serialization.

Tests the pure Python logic of _serialize_metadata and _deserialize_metadata
without requiring the Rust PyO3 library.
"""

from datetime import datetime
from typing import Any

import pytest

from nexus.contracts.metadata import DT_DIR, FileMetadata


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
        assert restored.is_dir == original.is_dir

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
            entry_type=DT_DIR,
            size=0,
        )
        data = _serialize_metadata(original)
        restored = _deserialize_metadata(data)

        assert restored.is_dir is True
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
