"""Unit tests for CacheConnectorMixin L1-only mode.

Tests cover the l1_only=True mode which skips L2 (PostgreSQL) caching entirely.
This mode is used by LocalConnector where the source is already local disk.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from nexus.backends.cache_mixin import CacheConnectorMixin


class MockL1OnlyBackend(CacheConnectorMixin):
    """Mock backend with l1_only=True for testing."""

    l1_only = True  # Skip L2 caching

    def __init__(self, physical_root: Path):
        self.name = "test_l1_only"
        self.physical_root = physical_root
        # No session_factory - L1-only mode doesn't need it

    def get_physical_path(self, virtual_path: str) -> Path:
        """Return physical path for L1 cache disk_path."""
        clean = virtual_path.lstrip("/")
        return self.physical_root / clean


class MockL1L2Backend(CacheConnectorMixin):
    """Mock backend with L1+L2 (default) for comparison."""

    l1_only = False  # Default: use L1+L2

    def __init__(self, session_factory):
        self.name = "test_l1_l2"
        self.session_factory = session_factory


class TestL1OnlyMode:
    """Test L1-only mode behavior."""

    def test_l1_only_has_caching_returns_true(self, tmp_path: Path):
        """l1_only backend should report caching as enabled."""
        backend = MockL1OnlyBackend(tmp_path)
        assert backend._has_caching() is True

    def test_l1_only_has_l2_caching_returns_false(self, tmp_path: Path):
        """l1_only backend should report L2 caching as disabled."""
        backend = MockL1OnlyBackend(tmp_path)
        assert backend._has_l2_caching() is False

    def test_l1_l2_has_caching_returns_true(self, tmp_path: Path):
        """L1+L2 backend with session_factory should report caching as enabled."""
        mock_session_factory = MagicMock()
        backend = MockL1L2Backend(mock_session_factory)
        assert backend._has_caching() is True

    def test_l1_l2_has_l2_caching_returns_true(self, tmp_path: Path):
        """L1+L2 backend with session_factory should report L2 caching as enabled."""
        mock_session_factory = MagicMock()
        backend = MockL1L2Backend(mock_session_factory)
        assert backend._has_l2_caching() is True

    def test_default_l1_only_is_false(self):
        """Default l1_only value should be False (regression guard)."""
        assert CacheConnectorMixin.l1_only is False

    def test_read_from_cache_skips_l2_in_l1_only_mode(self, tmp_path: Path):
        """_read_from_cache should skip L2 lookup when l1_only=True."""
        backend = MockL1OnlyBackend(tmp_path)

        # Mock L1 cache to return None (miss)
        with patch.object(backend, "_get_l1_cache") as mock_l1:
            mock_l1.return_value = None  # L1 disabled/miss

            # Should not call _get_db_session (which would fail without session_factory)
            result = backend._read_from_cache("/test/file.txt")
            assert result is None

    def test_write_to_cache_skips_l2_in_l1_only_mode(self, tmp_path: Path):
        """_write_to_cache should skip L2 write when l1_only=True."""
        # Create a test file
        test_file = tmp_path / "test.txt"
        test_file.write_bytes(b"test content")

        backend = MockL1OnlyBackend(tmp_path)

        # Mock L1 cache
        mock_l1_cache = MagicMock()
        with patch.object(backend, "_get_l1_cache", return_value=mock_l1_cache):
            entry = backend._write_to_cache(
                path="/test.txt",
                content=b"test content",
            )

            # Should return valid CacheEntry
            assert entry is not None
            assert entry.content_hash is not None
            assert entry.original_size == 12
            assert entry.path_id == "/test.txt"  # Uses path as path_id in L1-only mode
            assert entry.cache_id == ""  # No cache_id in L1-only mode

            # Should call L1 cache put with physical path as disk_path
            mock_l1_cache.put.assert_called_once()
            call_kwargs = mock_l1_cache.put.call_args.kwargs
            assert call_kwargs["key"] == "/test.txt"
            assert call_kwargs["disk_path"] == str(tmp_path / "test.txt")

    def test_write_to_cache_uses_get_physical_path(self, tmp_path: Path):
        """_write_to_cache should use get_physical_path() for disk_path in L1-only mode."""
        backend = MockL1OnlyBackend(tmp_path)

        # Create test file
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        test_file = subdir / "file.txt"
        test_file.write_bytes(b"nested content")

        mock_l1_cache = MagicMock()
        with patch.object(backend, "_get_l1_cache", return_value=mock_l1_cache):
            backend._write_to_cache(
                path="/subdir/file.txt",
                content=b"nested content",
            )

            # disk_path should be the physical path from get_physical_path()
            call_kwargs = mock_l1_cache.put.call_args.kwargs
            assert call_kwargs["disk_path"] == str(subdir / "file.txt")


class TestL1OnlyVsL1L2:
    """Test differences between L1-only and L1+L2 modes."""

    def test_l1_only_does_not_require_session_factory(self, tmp_path: Path):
        """L1-only backend should work without session_factory."""
        backend = MockL1OnlyBackend(tmp_path)

        # Should not raise even without session_factory
        assert backend._has_caching() is True
        assert backend._has_l2_caching() is False

    def test_l1_l2_requires_session_factory_for_l2(self):
        """L1+L2 backend without session_factory should report L2 as disabled."""

        class NoSessionBackend(CacheConnectorMixin):
            l1_only = False
            name = "no_session"

        backend = NoSessionBackend()
        assert backend._has_caching() is False
        assert backend._has_l2_caching() is False
