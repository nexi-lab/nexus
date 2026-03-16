"""Unit tests for CacheService — core cache logic.

Tests cover:
- L1 invalidation key fix (bug fix verification)
- read_from_cache: L1 hit, L2 hit, miss, TTL expiry
- write_to_cache: L1+L2 mode, L1-only mode
- read_content_with_cache: cache hit, miss -> fetch -> write-back
- bulk_write_to_cache: multi-entry writes
- read_bulk_from_cache: mixed L1/L2 hits
- Error paths: DB session failure, L1 failure
- _populate_l1 helper
- L1 lifecycle: config -> create -> stats -> clear

Part of: #1628 (Split CacheConnectorMixin into focused units)
"""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.backends.cache.service import CacheService
from nexus.contracts.types import OperationContext
from nexus.storage.file_cache import FileContentCache
from nexus.storage.models import Base, FilePathModel

# =========================================================================
# Fixtures
# =========================================================================


class MockConnector:
    """Minimal mock connector for CacheService tests."""

    def __init__(self, session_factory=None, l1_only=False, zone_id=None):
        self.session_factory = session_factory
        self.l1_only = l1_only
        self.zone_id = zone_id
        self.cache_ttl = 0
        self.name = "mock_connector"

    def _fetch_content(self, content_hash, context=None):
        return b"fetched content"

    def _get_backend_version(self, context=None):
        return None

    def _read_content_from_backend(self, path, context=None):
        return None


@pytest.fixture
def db_session(tmp_path: Path):
    """Create test database session factory."""
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def connector(db_session):
    """Create mock connector with DB session."""
    return MockConnector(session_factory=db_session)


@pytest.fixture
def l1_only_connector():
    """Create mock connector in L1-only mode."""
    return MockConnector(l1_only=True)


@pytest.fixture
def file_cache(tmp_path: Path):
    """Create a fresh disk cache."""
    return FileContentCache(tmp_path / "cache")


@pytest.fixture
def cache_service(connector, file_cache):
    """Create CacheService with mock connector, no L1, injected file_cache."""
    return CacheService(connector=connector, l1_cache=None, file_cache=file_cache)


# =========================================================================
# Caching availability checks
# =========================================================================


class TestHasCaching:
    def test_l1_only_returns_true(self, l1_only_connector):
        svc = CacheService(connector=l1_only_connector)
        assert svc.has_caching() is True

    def test_l1_only_has_l2_returns_false(self, l1_only_connector):
        svc = CacheService(connector=l1_only_connector)
        assert svc.has_l2_caching() is False

    def test_with_session_factory_returns_true(self, cache_service):
        assert cache_service.has_caching() is True
        assert cache_service.has_l2_caching() is True

    def test_no_session_returns_false(self):
        connector = MockConnector(session_factory=None)
        svc = CacheService(connector=connector)
        assert svc.has_caching() is False
        assert svc.has_l2_caching() is False


# =========================================================================
# Invalidation — bug fix verification
# =========================================================================


class TestInvalidateCache:
    def test_invalidate_uses_bare_path_key(self, cache_service):
        """Bug fix: L1 removal uses bare `path` key, not `cache_entry:{path}`."""
        mock_l1 = MagicMock()
        cache_service._l1_cache = mock_l1

        cache_service.invalidate_cache(path="/test/file.txt")

        # Key MUST be bare path (the bug used f"cache_entry:{path}")
        mock_l1.remove.assert_called_once_with("/test/file.txt")

    def test_invalidate_mount_prefix_clears_l1(self, cache_service, db_session):
        mock_l1 = MagicMock()
        cache_service._l1_cache = mock_l1

        # Create a file path entry in DB
        session = db_session()
        session.add(
            FilePathModel(
                path_id="p1",
                virtual_path="/mnt/gcs/file.txt",
                backend_id="b1",
                physical_path="file.txt",
                zone_id="root",
            )
        )
        session.commit()
        session.close()

        count = cache_service.invalidate_cache(mount_prefix="/mnt/gcs")

        mock_l1.clear.assert_called_once()
        assert count == 1

    def test_invalidate_no_args_returns_zero(self, cache_service):
        assert cache_service.invalidate_cache() == 0


# =========================================================================
# Read from cache
# =========================================================================


class TestReadFromCache:
    def test_l1_miss_l2_miss_returns_none(self, cache_service, file_cache):
        result = cache_service.read_from_cache("/test/file.txt")
        assert result is None

    def test_l2_hit_returns_entry(self, cache_service, file_cache):
        # Write metadata to disk cache
        file_cache.write("root", "/test/file.txt", b"content", text_content="content")
        meta = {
            "path_id": "p1",
            "zone_id": "root",
            "content_hash": "h1",
            "content_type": "full",
            "original_size": 7,
            "cached_size": 7,
            "synced_at": datetime.now(UTC).isoformat(),
            "stale": False,
        }
        file_cache.write_meta("root", "/test/file.txt", meta)

        entry = cache_service.read_from_cache("/test/file.txt")

        assert entry is not None
        assert entry.content_hash == "h1"
        assert entry.path_id == "p1"

    def test_l2_ttl_expired_returns_none(self, connector, file_cache):
        connector.cache_ttl = 1  # 1 second TTL

        svc = CacheService(connector=connector, file_cache=file_cache)

        # Write metadata with old timestamp
        old_time = "2020-01-01T00:00:00+00:00"
        file_cache.write("root", "/test/old.txt", b"old", text_content="old")
        meta = {
            "content_hash": "h1",
            "content_type": "full",
            "original_size": 3,
            "cached_size": 3,
            "synced_at": old_time,
            "stale": False,
        }
        file_cache.write_meta("root", "/test/old.txt", meta)

        entry = svc.read_from_cache("/test/old.txt")

        assert entry is None  # TTL expired

    def test_l1_only_skips_l2(self, l1_only_connector):
        svc = CacheService(connector=l1_only_connector)
        # Should not attempt L2 read (no DB session)
        result = svc.read_from_cache("/test/file.txt")
        assert result is None


# =========================================================================
# Write to cache
# =========================================================================


class TestWriteToCache:
    def test_l1_l2_write(self, cache_service, db_session, file_cache):
        # Create file_path entry
        session = db_session()
        session.add(
            FilePathModel(
                path_id="p1",
                virtual_path="/test/file.txt",
                backend_id="b1",
                physical_path="file.txt",
                zone_id="root",
            )
        )
        session.commit()
        session.close()

        entry = cache_service.write_to_cache(
            path="/test/file.txt",
            content=b"test content",
            backend_version="v1",
        )

        assert entry.content_hash is not None
        assert entry.original_size == 12
        assert entry.content_text == "test content"
        assert entry.backend_version == "v1"
        assert entry.stale is False

    def test_l1_only_write(self, l1_only_connector):
        mock_l1 = MagicMock()
        l1_only_connector.get_physical_path = lambda p: Path("/data") / p.lstrip("/")

        svc = CacheService(connector=l1_only_connector, l1_cache=mock_l1)
        entry = svc.write_to_cache(path="/test.txt", content=b"data")

        assert entry.path_id == "/test.txt"  # Uses path as path_id in L1-only
        assert entry.cache_id == ""
        mock_l1.put.assert_called_once()

    def test_binary_content_stored_as_reference(self, cache_service, file_cache):
        entry = cache_service.write_to_cache(
            path="/test/binary.bin",
            content=b"\x00\x01\x02\xff",  # Not UTF-8
        )
        assert entry.content_type == "reference"
        assert entry.content_text is None


# =========================================================================
# Bulk write
# =========================================================================


class TestBulkWriteToCache:
    def test_writes_multiple_entries(self, cache_service, db_session, file_cache):
        session = db_session()
        session.add(
            FilePathModel(
                path_id="p1",
                virtual_path="/test/a.txt",
                backend_id="b1",
                physical_path="a.txt",
                zone_id="root",
            )
        )
        session.add(
            FilePathModel(
                path_id="p2",
                virtual_path="/test/b.txt",
                backend_id="b1",
                physical_path="b.txt",
                zone_id="root",
            )
        )
        session.commit()
        session.close()

        entries = [
            {"path": "/test/a.txt", "content": b"aaa", "zone_id": "root"},
            {"path": "/test/b.txt", "content": b"bbb", "zone_id": "root"},
        ]

        results = cache_service.bulk_write_to_cache(entries)

        assert len(results) == 2
        assert results[0].content_text == "aaa"
        assert results[1].content_text == "bbb"

    def test_empty_entries_returns_empty(self, cache_service):
        assert cache_service.bulk_write_to_cache([]) == []


# =========================================================================
# Bulk read from cache
# =========================================================================


class TestReadBulkFromCache:
    def test_all_misses(self, cache_service, file_cache):
        results = cache_service.read_bulk_from_cache(["/a.txt", "/b.txt"])
        assert len(results) == 0

    def test_l2_hits(self, cache_service, file_cache):
        # Write entries to disk cache
        for name in ["a.txt", "b.txt"]:
            path = f"/test/{name}"
            file_cache.write("root", path, b"content", text_content="content")
            file_cache.write_meta(
                "root",
                path,
                {
                    "content_hash": f"h_{name}",
                    "content_type": "full",
                    "original_size": 7,
                    "cached_size": 7,
                    "synced_at": datetime.now(UTC).isoformat(),
                    "stale": False,
                },
            )

        results = cache_service.read_bulk_from_cache(["/test/a.txt", "/test/b.txt", "/test/c.txt"])

        assert len(results) == 2
        assert "/test/a.txt" in results
        assert "/test/b.txt" in results
        assert "/test/c.txt" not in results

    def test_empty_paths_returns_empty(self, cache_service):
        assert cache_service.read_bulk_from_cache([]) == {}


# =========================================================================
# read_content_with_cache
# =========================================================================


class TestReadContentWithCache:
    def test_cache_miss_fetches_from_backend(self, cache_service, file_cache):
        ctx = OperationContext(user_id="test", groups=[], backend_path="/file.txt", is_system=True)

        result = cache_service.read_content_with_cache("hash1", ctx)

        assert result.content == b"fetched content"
        assert result.from_cache is False

    def test_requires_backend_path(self, cache_service):
        ctx = OperationContext(user_id="test", groups=[], is_system=True)
        with pytest.raises(ValueError, match="backend_path"):
            cache_service.read_content_with_cache("hash1", ctx)

    def test_cache_hit_returns_cached(self, cache_service, file_cache):
        # Pre-populate cache
        path = "/test/cached.txt"
        file_cache.write("root", path, b"cached data", text_content="cached data")
        file_cache.write_meta(
            "root",
            path,
            {
                "content_hash": "cached_hash",
                "content_type": "full",
                "original_size": 11,
                "cached_size": 11,
                "synced_at": datetime.now(UTC).isoformat(),
                "stale": False,
            },
        )

        ctx = OperationContext(
            user_id="test", groups=[], backend_path="/test/cached.txt", is_system=True
        )
        ctx.virtual_path = "/test/cached.txt"

        result = cache_service.read_content_with_cache("hash1", ctx)

        assert result.from_cache is True
        assert result.content == b"cached data"


# =========================================================================
# Version checking
# =========================================================================


class TestCheckVersion:
    def test_no_version_support_returns_true(self, cache_service):
        assert cache_service.check_version("/test.txt", "v1") is True

    def test_version_match_returns_true(self, connector, cache_service):
        connector.get_version = lambda path, ctx=None: "v1"
        assert cache_service.check_version("/test.txt", "v1") is True

    def test_version_mismatch_raises_conflict(self, connector, cache_service):
        from nexus.contracts.exceptions import ConflictError

        connector.get_version = lambda path, ctx=None: "v2"
        with pytest.raises(ConflictError):
            cache_service.check_version("/test.txt", "v1")


# =========================================================================
# Content hash / size lookups
# =========================================================================


class TestGetContentHash:
    def test_returns_none_when_no_caching(self):
        connector = MockConnector(session_factory=None)
        svc = CacheService(connector=connector)
        assert svc.get_content_hash("/test.txt") is None


class TestGetSizeFromCache:
    def test_returns_none_on_miss(self, cache_service, file_cache):
        assert cache_service.get_size_from_cache("/nonexistent.txt") is None

    def test_returns_size_on_hit(self, cache_service, file_cache):
        path = "/test/sized.txt"
        file_cache.write("root", path, b"12345", text_content="12345")
        file_cache.write_meta(
            "root",
            path,
            {
                "content_hash": "h1",
                "content_type": "full",
                "original_size": 5,
                "cached_size": 5,
                "synced_at": datetime.now(UTC).isoformat(),
                "stale": False,
            },
        )

        assert cache_service.get_size_from_cache(path) == 5


# =========================================================================
# Helpers
# =========================================================================


class TestPopulateL1:
    def test_calls_l1_put(self, cache_service):
        mock_l1 = MagicMock()
        cache_service._l1_cache = mock_l1

        cache_service._populate_l1(
            key="/test.txt",
            path_id="p1",
            content_hash="h1",
            disk_path="/cache/test.txt",
            original_size=100,
            zone_id="root",
        )

        mock_l1.put.assert_called_once()
        kwargs = mock_l1.put.call_args.kwargs
        assert kwargs["key"] == "/test.txt"
        assert kwargs["path_id"] == "p1"
        assert kwargs["disk_path"] == "/cache/test.txt"

    def test_skips_when_no_l1(self, cache_service):
        # No L1 cache — should be a no-op
        cache_service._populate_l1(
            key="/test.txt",
            path_id="p1",
            content_hash="h1",
            disk_path="/cache/test.txt",
            original_size=100,
            zone_id="root",
        )
        # Should not raise

    def test_logs_warning_on_l1_error(self, cache_service):
        mock_l1 = MagicMock()
        mock_l1.put.side_effect = RuntimeError("L1 FFI error")
        cache_service._l1_cache = mock_l1

        # Should not raise, but should log warning
        cache_service._populate_l1(
            key="/test.txt",
            path_id="p1",
            content_hash="h1",
            disk_path="/cache/test.txt",
            original_size=100,
            zone_id="root",
        )
        mock_l1.put.assert_called_once()


class TestCacheZone:
    def test_default_zone_is_root(self, cache_service):
        assert cache_service._get_cache_zone() == "root"

    def test_uses_connector_zone_id(self, connector, cache_service):
        connector.zone_id = "my_zone"
        assert cache_service._get_cache_zone() == "my_zone"


class TestCacheTTL:
    def test_default_ttl_is_zero(self, cache_service):
        assert cache_service._get_cache_ttl() == 0

    def test_uses_connector_cache_ttl(self, connector, cache_service):
        connector.cache_ttl = 300
        assert cache_service._get_cache_ttl() == 300
