"""Tests for per-request consistency modes (Issue #1081).

Tests the SpiceDB/Zanzibar-aligned consistency modes:
- MINIMIZE_LATENCY: Use cache for fastest response
- AT_LEAST_AS_FRESH: Cache must be >= min_revision
- FULLY_CONSISTENT: Bypass cache entirely

References:
- https://authzed.com/docs/spicedb/concepts/consistency
- https://www.usenix.org/system/files/atc19-pang.pdf (Zanzibar paper)
"""

import pytest

from nexus.core.rebac_cache import ReBACPermissionCache
from nexus.core.rebac_manager_enhanced import (
    ConsistencyLevel,
    ConsistencyMode,
    ConsistencyRequirement,
    WriteResult,
)


class TestConsistencyModeEnum:
    """Tests for ConsistencyMode enum."""

    def test_mode_values(self):
        """Test that mode values match SpiceDB naming."""
        assert ConsistencyMode.MINIMIZE_LATENCY.value == "minimize_latency"
        assert ConsistencyMode.AT_LEAST_AS_FRESH.value == "at_least_as_fresh"
        assert ConsistencyMode.FULLY_CONSISTENT.value == "fully_consistent"

    def test_all_modes_exist(self):
        """Test that all expected modes are defined."""
        modes = [m.value for m in ConsistencyMode]
        assert "minimize_latency" in modes
        assert "at_least_as_fresh" in modes
        assert "fully_consistent" in modes


class TestConsistencyRequirement:
    """Tests for ConsistencyRequirement dataclass."""

    def test_default_mode(self):
        """Test default mode is MINIMIZE_LATENCY."""
        req = ConsistencyRequirement()
        assert req.mode == ConsistencyMode.MINIMIZE_LATENCY
        assert req.min_revision is None

    def test_minimize_latency_mode(self):
        """Test MINIMIZE_LATENCY mode creation."""
        req = ConsistencyRequirement(mode=ConsistencyMode.MINIMIZE_LATENCY)
        assert req.mode == ConsistencyMode.MINIMIZE_LATENCY
        assert req.min_revision is None

    def test_fully_consistent_mode(self):
        """Test FULLY_CONSISTENT mode creation."""
        req = ConsistencyRequirement(mode=ConsistencyMode.FULLY_CONSISTENT)
        assert req.mode == ConsistencyMode.FULLY_CONSISTENT
        assert req.min_revision is None

    def test_at_least_as_fresh_requires_revision(self):
        """Test AT_LEAST_AS_FRESH mode requires min_revision."""
        with pytest.raises(ValueError) as exc_info:
            ConsistencyRequirement(mode=ConsistencyMode.AT_LEAST_AS_FRESH)
        assert "min_revision is required" in str(exc_info.value)

    def test_at_least_as_fresh_with_revision(self):
        """Test AT_LEAST_AS_FRESH mode with min_revision."""
        req = ConsistencyRequirement(
            mode=ConsistencyMode.AT_LEAST_AS_FRESH,
            min_revision=42
        )
        assert req.mode == ConsistencyMode.AT_LEAST_AS_FRESH
        assert req.min_revision == 42

    def test_to_legacy_level_minimize_latency(self):
        """Test conversion to legacy ConsistencyLevel."""
        req = ConsistencyRequirement(mode=ConsistencyMode.MINIMIZE_LATENCY)
        assert req.to_legacy_level() == ConsistencyLevel.EVENTUAL

    def test_to_legacy_level_at_least_as_fresh(self):
        """Test conversion to legacy ConsistencyLevel."""
        req = ConsistencyRequirement(
            mode=ConsistencyMode.AT_LEAST_AS_FRESH,
            min_revision=1
        )
        assert req.to_legacy_level() == ConsistencyLevel.BOUNDED

    def test_to_legacy_level_fully_consistent(self):
        """Test conversion to legacy ConsistencyLevel."""
        req = ConsistencyRequirement(mode=ConsistencyMode.FULLY_CONSISTENT)
        assert req.to_legacy_level() == ConsistencyLevel.STRONG


class TestWriteResult:
    """Tests for WriteResult dataclass."""

    def test_write_result_creation(self):
        """Test WriteResult creation."""
        result = WriteResult(
            tuple_id="abc-123",
            revision=42,
            consistency_token="v42",
            written_at_ms=1.5
        )
        assert result.tuple_id == "abc-123"
        assert result.revision == 42
        assert result.consistency_token == "v42"
        assert result.written_at_ms == 1.5


class TestReBACPermissionCacheRevisionCheck:
    """Tests for revision-aware cache lookup (Issue #1081)."""

    def test_get_with_revision_check_no_entry(self):
        """Test get_with_revision_check returns None for missing entry."""
        cache = ReBACPermissionCache()
        result, revision = cache.get_with_revision_check(
            "user", "alice", "read", "file", "/doc.txt",
            tenant_id="default",
            min_revision=None
        )
        assert result is None
        assert revision == 0

    def test_get_with_revision_check_cached_entry(self):
        """Test get_with_revision_check returns cached entry when fresh enough."""
        cache = ReBACPermissionCache()

        # Mock the revision fetcher to return revision 100
        cache.set_revision_fetcher(lambda _tenant_id: 100)

        # Cache an entry
        cache.set(
            "user", "alice", "read", "file", "/doc.txt",
            result=True,
            tenant_id="default"
        )

        # Get with min_revision <= current revision should succeed
        result, revision = cache.get_with_revision_check(
            "user", "alice", "read", "file", "/doc.txt",
            tenant_id="default",
            min_revision=50  # Less than 100
        )
        assert result is True
        assert revision == 100

    def test_get_with_revision_check_stale_entry(self):
        """Test get_with_revision_check returns None when entry is too stale."""
        cache = ReBACPermissionCache()

        # Mock revision fetcher - start at 100
        cache.set_revision_fetcher(lambda _tenant_id: 100)

        # Cache an entry at revision 100
        cache.set(
            "user", "alice", "read", "file", "/doc.txt",
            result=True,
            tenant_id="default"
        )

        # Get with min_revision > cached revision should return None
        result, revision = cache.get_with_revision_check(
            "user", "alice", "read", "file", "/doc.txt",
            tenant_id="default",
            min_revision=150  # Greater than 100
        )
        assert result is None
        assert revision == 100  # Returns the cached revision even though stale

    def test_get_with_revision_check_no_min_revision(self):
        """Test get_with_revision_check without min_revision uses normal lookup."""
        cache = ReBACPermissionCache()

        cache.set_revision_fetcher(lambda _tenant_id: 100)

        cache.set(
            "user", "alice", "read", "file", "/doc.txt",
            result=True,
            tenant_id="default"
        )

        # Without min_revision, should return cached entry regardless of revision
        result, _revision = cache.get_with_revision_check(
            "user", "alice", "read", "file", "/doc.txt",
            tenant_id="default",
            min_revision=None
        )
        assert result is True

    def test_entry_metadata_includes_revision(self):
        """Test that cached entries store revision in metadata."""
        cache = ReBACPermissionCache()
        cache.set_revision_fetcher(lambda _tenant_id: 42)

        cache.set(
            "user", "alice", "read", "file", "/doc.txt",
            result=True,
            tenant_id="default"
        )

        # Check internal metadata has 4 elements (including revision)
        key = cache._make_key("user", "alice", "read", "file", "/doc.txt", "default")
        metadata = cache._entry_metadata.get(key)
        assert metadata is not None
        assert len(metadata) == 4  # (created_at, ttl, delta, revision)
        assert metadata[3] == 42  # revision
