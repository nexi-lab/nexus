"""Unit tests for cache_models.py — CacheEntry factories and data classes.

Part of: #1628 (Split CacheConnectorMixin into focused units)
"""

from datetime import UTC, datetime

from nexus.backends.cache.models import (
    IMMUTABLE_VERSION,
    MAX_CACHE_FILE_SIZE,
    MAX_FULL_TEXT_SIZE,
    SUMMARY_SIZE,
    CachedReadResult,
    CacheEntry,
    SyncResult,
)


class TestConstants:
    """Test that constants have expected values."""

    def test_immutable_version(self):
        assert IMMUTABLE_VERSION == "immutable"

    def test_max_cache_file_size(self):
        assert MAX_CACHE_FILE_SIZE == 100 * 1024 * 1024

    def test_max_full_text_size(self):
        assert MAX_FULL_TEXT_SIZE == 10 * 1024 * 1024

    def test_summary_size(self):
        assert SUMMARY_SIZE == 100 * 1024


class TestSyncResult:
    """Test SyncResult dataclass."""

    def test_defaults(self):
        result = SyncResult()
        assert result.files_scanned == 0
        assert result.files_synced == 0
        assert result.files_skipped == 0
        assert result.bytes_synced == 0
        assert result.embeddings_generated == 0
        assert result.errors == []

    def test_repr_format(self):
        result = SyncResult(files_scanned=10, files_synced=5, errors=["err1", "err2"])
        r = repr(result)
        assert "scanned=10" in r
        assert "synced=5" in r
        assert "errors=2" in r


class TestCacheEntryLazyLoading:
    """Test CacheEntry lazy loading behavior."""

    def test_content_binary_lazy_load(self):
        entry = CacheEntry(
            cache_id="",
            path_id="p1",
            content_text=None,
            _content_binary=None,
            content_hash="hash1",
            content_type="full",
            original_size=5,
            cached_size=0,
            backend_version=None,
            synced_at=datetime.now(UTC),
            stale=False,
            _content_binary_raw=b"hello",
        )
        assert entry._content_binary is None
        # First access triggers lazy load
        assert entry.content_binary == b"hello"
        assert entry._content_binary == b"hello"

    def test_content_binary_setter_clears_raw(self):
        entry = CacheEntry(
            cache_id="",
            path_id="p1",
            content_text=None,
            _content_binary=None,
            content_hash="hash1",
            content_type="full",
            original_size=5,
            cached_size=0,
            backend_version=None,
            synced_at=datetime.now(UTC),
            stale=False,
            _content_binary_raw=b"raw",
        )
        entry.content_binary = b"direct"
        assert entry._content_binary == b"direct"
        assert entry._content_binary_raw is None


class TestCacheEntryFromL1Content:
    """Test CacheEntry.from_l1_content() factory."""

    def test_creates_entry_from_l1_content(self):
        now = datetime.now(UTC)
        entry = CacheEntry.from_l1_content(b"hello world", "abc123", now)
        assert entry.cache_id == ""
        assert entry.path_id == ""
        assert entry.content_binary == b"hello world"
        assert entry.content_hash == "abc123"
        assert entry.content_type == "full"
        assert entry.original_size == 11
        assert entry.cached_size == 11
        assert entry.stale is False
        assert entry.synced_at == now

    def test_defaults_to_utc_now(self):
        before = datetime.now(UTC)
        entry = CacheEntry.from_l1_content(b"test", "hash1")
        after = datetime.now(UTC)
        assert before <= entry.synced_at <= after


class TestCacheEntryFromL1Metadata:
    """Test CacheEntry.from_l1_metadata() factory."""

    def test_creates_entry_from_metadata(self):
        now = datetime.now(UTC)
        entry = CacheEntry.from_l1_metadata("pid1", "hash1", 1024, now)
        assert entry.path_id == "pid1"
        assert entry.content_hash == "hash1"
        assert entry.original_size == 1024
        assert entry.content_binary is None
        assert entry.cached_size == 0
        assert entry.stale is False


class TestCacheEntryFromDiskMeta:
    """Test CacheEntry.from_disk_meta() factory."""

    def test_creates_entry_from_disk_meta(self):
        meta = {
            "path_id": "p1",
            "content_hash": "h1",
            "content_type": "parsed",
            "original_size": 2048,
            "cached_size": 1024,
            "backend_version": "v2",
            "synced_at": "2024-01-15T10:30:00+00:00",
            "stale": False,
            "parsed_from": "pdf",
            "parse_metadata": {"chunks": 5},
        }
        entry = CacheEntry.from_disk_meta(meta, "text content", b"binary")
        assert entry.path_id == "p1"
        assert entry.content_text == "text content"
        assert entry.content_hash == "h1"
        assert entry.content_type == "parsed"
        assert entry.original_size == 2048
        assert entry.parsed_from == "pdf"
        assert entry.parse_metadata == {"chunks": 5}
        assert entry._content_binary_raw == b"binary"
        # Lazy loading: binary not set until accessed
        assert entry._content_binary is None
        assert entry.content_binary == b"binary"

    def test_missing_synced_at_defaults_to_now(self):
        meta = {"content_hash": "h1"}
        before = datetime.now(UTC)
        entry = CacheEntry.from_disk_meta(meta)
        after = datetime.now(UTC)
        assert before <= entry.synced_at <= after

    def test_missing_fields_get_defaults(self):
        meta = {}
        entry = CacheEntry.from_disk_meta(meta)
        assert entry.path_id == ""
        assert entry.content_hash == ""
        assert entry.content_type == "full"
        assert entry.original_size == 0
        assert entry.cached_size == 0
        assert entry.backend_version is None
        assert entry.stale is False
        assert entry.parsed_from is None
        assert entry.parse_metadata is None


class TestCacheEntryFromWrite:
    """Test CacheEntry.from_write() factory."""

    def test_creates_entry_from_write(self):
        now = datetime.now(UTC)
        entry = CacheEntry.from_write(
            path_id="p1",
            content=b"file content",
            content_hash="hash1",
            content_text="file content",
            content_type="full",
            original_size=12,
            cached_size=12,
            backend_version="v1",
            parsed_from="txt",
            parse_metadata={"lines": 1},
            now=now,
        )
        assert entry.path_id == "p1"
        assert entry.content_hash == "hash1"
        assert entry.content_binary == b"file content"
        assert entry.content_text == "file content"
        assert entry.content_type == "full"
        assert entry.original_size == 12
        assert entry.cached_size == 12
        assert entry.backend_version == "v1"
        assert entry.stale is False
        assert entry.synced_at == now

    def test_large_content_not_stored_in_binary(self):
        large_content = b"x" * (MAX_CACHE_FILE_SIZE + 1)
        entry = CacheEntry.from_write(
            path_id="p1",
            content=large_content,
            content_hash="hash1",
            content_text=None,
            content_type="reference",
            original_size=len(large_content),
            cached_size=0,
            max_cache_file_size=MAX_CACHE_FILE_SIZE,
        )
        # Content too large — should not be stored in _content_binary
        assert entry.content_binary is None


class TestCachedReadResult:
    """Test CachedReadResult dataclass."""

    def test_basic_creation(self):
        result = CachedReadResult(
            content=b"data",
            content_hash="hash1",
            from_cache=True,
        )
        assert result.content == b"data"
        assert result.content_hash == "hash1"
        assert result.from_cache is True
        assert result.cache_entry is None
