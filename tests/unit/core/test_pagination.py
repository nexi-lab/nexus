"""Unit tests for pagination utilities (Issue #937)."""

import pytest

from nexus.core.pagination import (
    CursorData,
    CursorError,
    _hash_filters,
    decode_cursor,
    encode_cursor,
)


class TestCursorEncoding:
    """Tests for cursor encode/decode."""

    def test_encode_decode_roundtrip(self):
        """Cursor should survive encode/decode cycle."""
        filters = {"prefix": "/workspace/", "recursive": True, "zone_id": "org_123"}

        cursor = encode_cursor(
            last_path="/workspace/file999.txt",
            last_path_id="uuid-123",
            filters=filters,
        )

        decoded = decode_cursor(cursor, filters)

        assert decoded.path == "/workspace/file999.txt"
        assert decoded.path_id == "uuid-123"

    def test_encode_decode_with_none_path_id(self):
        """Cursor should work with None path_id."""
        filters = {"prefix": "/", "recursive": True, "zone_id": None}

        cursor = encode_cursor(
            last_path="/file.txt",
            last_path_id=None,
            filters=filters,
        )

        decoded = decode_cursor(cursor, filters)

        assert decoded.path == "/file.txt"
        assert decoded.path_id is None

    def test_decode_detects_filter_tampering(self):
        """Cursor should reject if query filters changed."""
        original_filters = {"prefix": "/a/", "recursive": True, "zone_id": None}
        cursor = encode_cursor("/a/file.txt", "id1", original_filters)

        tampered_filters = {"prefix": "/b/", "recursive": True, "zone_id": None}

        with pytest.raises(CursorError, match="filters mismatch"):
            decode_cursor(cursor, tampered_filters)

    def test_decode_detects_recursive_change(self):
        """Cursor should reject if recursive flag changed."""
        original_filters = {"prefix": "/a/", "recursive": True, "zone_id": None}
        cursor = encode_cursor("/a/file.txt", "id1", original_filters)

        changed_filters = {"prefix": "/a/", "recursive": False, "zone_id": None}

        with pytest.raises(CursorError, match="filters mismatch"):
            decode_cursor(cursor, changed_filters)

    def test_decode_detects_zone_change(self):
        """Cursor should reject if zone_id changed."""
        original_filters = {"prefix": "/a/", "recursive": True, "zone_id": "org1"}
        cursor = encode_cursor("/a/file.txt", "id1", original_filters)

        changed_filters = {"prefix": "/a/", "recursive": True, "zone_id": "org2"}

        with pytest.raises(CursorError, match="filters mismatch"):
            decode_cursor(cursor, changed_filters)

    def test_decode_malformed_cursor(self):
        """Should raise CursorError for invalid base64."""
        with pytest.raises(CursorError, match="Malformed"):
            decode_cursor("not-valid-base64!!!", {})

    def test_decode_invalid_json(self):
        """Should raise CursorError for invalid JSON in cursor."""
        import base64

        bad_cursor = base64.urlsafe_b64encode(b"not json").decode()

        with pytest.raises(CursorError, match="Malformed"):
            decode_cursor(bad_cursor, {})

    def test_decode_missing_fields(self):
        """Should raise CursorError if required fields missing."""
        import base64
        import json

        # Missing "p" (path) field
        bad_cursor = base64.urlsafe_b64encode(json.dumps({"h": "somehash"}).encode()).decode()

        with pytest.raises(CursorError, match="missing required"):
            decode_cursor(bad_cursor, {})

    def test_decode_missing_hash_field(self):
        """Should raise CursorError if hash field missing."""
        import base64
        import json

        # Missing "h" (hash) field
        bad_cursor = base64.urlsafe_b64encode(json.dumps({"p": "/file.txt"}).encode()).decode()

        with pytest.raises(CursorError, match="missing required"):
            decode_cursor(bad_cursor, {})

    def test_cursor_is_url_safe(self):
        """Cursor should be URL-safe base64."""
        filters = {"prefix": "/path/with/slashes/", "recursive": True, "zone_id": None}
        cursor = encode_cursor("/path/with/slashes/file.txt", "uuid", filters)

        # Should not contain +, /, or = (URL-unsafe chars in standard base64)
        # urlsafe_b64encode uses - and _ instead
        assert "+" not in cursor
        assert "/" not in cursor
        # Note: = is allowed at the end for padding

    def test_cursor_with_unicode_path(self):
        """Cursor should handle unicode paths."""
        filters = {"prefix": "/文档/", "recursive": True, "zone_id": None}
        cursor = encode_cursor("/文档/文件.txt", "uuid", filters)

        decoded = decode_cursor(cursor, filters)
        assert decoded.path == "/文档/文件.txt"

    def test_cursor_with_special_chars(self):
        """Cursor should handle special characters in path."""
        filters = {"prefix": "/test/", "recursive": True, "zone_id": None}
        path = "/test/file with spaces & symbols!.txt"
        cursor = encode_cursor(path, "uuid", filters)

        decoded = decode_cursor(cursor, filters)
        assert decoded.path == path


class TestFilterHashing:
    """Tests for filter hash stability."""

    def test_hash_deterministic(self):
        """Same filters should produce same hash."""
        filters1 = {"a": 1, "b": 2}
        filters2 = {"b": 2, "a": 1}  # Different order

        assert _hash_filters(filters1) == _hash_filters(filters2)

    def test_hash_differs_for_different_filters(self):
        """Different filters should produce different hash."""
        filters1 = {"prefix": "/a/"}
        filters2 = {"prefix": "/b/"}

        assert _hash_filters(filters1) != _hash_filters(filters2)

    def test_hash_differs_for_different_values(self):
        """Same keys with different values should produce different hash."""
        filters1 = {"recursive": True}
        filters2 = {"recursive": False}

        assert _hash_filters(filters1) != _hash_filters(filters2)

    def test_hash_length(self):
        """Hash should be truncated to 16 characters."""
        filters = {"prefix": "/test/", "recursive": True, "zone_id": "org123"}
        hash_result = _hash_filters(filters)

        assert len(hash_result) == 16

    def test_hash_is_hex(self):
        """Hash should be hexadecimal."""
        filters = {"prefix": "/test/"}
        hash_result = _hash_filters(filters)

        # Should only contain hex chars
        assert all(c in "0123456789abcdef" for c in hash_result)

    def test_hash_with_none_values(self):
        """Hash should handle None values."""
        filters1 = {"prefix": "/", "zone_id": None}
        filters2 = {"prefix": "/", "zone_id": None}

        assert _hash_filters(filters1) == _hash_filters(filters2)

    def test_hash_none_vs_missing(self):
        """Hash should differ for None vs missing key."""
        filters1 = {"prefix": "/", "zone_id": None}
        filters2 = {"prefix": "/"}

        assert _hash_filters(filters1) != _hash_filters(filters2)


class TestCursorData:
    """Tests for CursorData dataclass."""

    def test_cursor_data_creation(self):
        """CursorData should store all fields."""
        data = CursorData(
            path="/test/file.txt",
            path_id="uuid-123",
            filters_hash="abc123",
        )

        assert data.path == "/test/file.txt"
        assert data.path_id == "uuid-123"
        assert data.filters_hash == "abc123"

    def test_cursor_data_with_none_path_id(self):
        """CursorData should allow None path_id."""
        data = CursorData(
            path="/test/file.txt",
            path_id=None,
            filters_hash="abc123",
        )

        assert data.path_id is None
