"""Unit tests for GovernanceGraphService cache behavior.

Issue #2129 §10A: Tests for TTL expiry, cache size limit,
reverse invalidation, and periodic sweep.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from nexus.bricks.governance.governance_graph_service import GovernanceGraphService
from nexus.bricks.governance.models import ConstraintCheckResult


def _make_service(cache_ttl: float = 60.0) -> GovernanceGraphService:
    """Create a GovernanceGraphService with a mock session factory."""
    session_factory = MagicMock()
    return GovernanceGraphService(session_factory=session_factory, cache_ttl=cache_ttl)


class TestCacheTTLExpiry:
    @patch("nexus.bricks.governance.governance_graph_service.time")
    async def test_expired_entry_triggers_db_lookup(self, mock_time: MagicMock) -> None:
        """When a cached entry has expired, the service should re-fetch from DB."""
        svc = _make_service(cache_ttl=10.0)

        # Seed cache with an entry that expires at t=110
        result = ConstraintCheckResult(allowed=True)
        svc._cache[("z1", "a", "b")] = (result, 110.0)

        # At t=120, the entry is expired
        mock_time.monotonic.return_value = 120.0

        # Mock the DB lookup
        new_result = ConstraintCheckResult(allowed=False, reason="blocked")
        svc._lookup_constraint = AsyncMock(return_value=new_result)

        got = await svc.check_constraint("a", "b", "z1")
        assert got.allowed is False
        svc._lookup_constraint.assert_awaited_once()

    @patch("nexus.bricks.governance.governance_graph_service.time")
    async def test_valid_entry_returns_cached(self, mock_time: MagicMock) -> None:
        """When a cached entry is still valid, the service should return it without DB lookup."""
        svc = _make_service(cache_ttl=60.0)

        result = ConstraintCheckResult(allowed=True)
        svc._cache[("z1", "a", "b")] = (result, 200.0)

        mock_time.monotonic.return_value = 150.0  # Before expiry

        svc._lookup_constraint = AsyncMock()

        got = await svc.check_constraint("a", "b", "z1")
        assert got.allowed is True
        svc._lookup_constraint.assert_not_awaited()


class TestCacheSizeLimit:
    @patch("nexus.bricks.governance.governance_graph_service.time")
    async def test_cache_full_evicts_oldest_entry(self, mock_time: MagicMock) -> None:
        """When cache is at max size, the oldest entry is evicted (LRU)."""
        svc = _make_service()
        svc._CACHE_MAX_SIZE = 2

        mock_time.monotonic.return_value = 100.0

        # Fill cache: (a,b) expires earlier than (c,d)
        r = ConstraintCheckResult(allowed=True)
        svc._cache[("z1", "a", "b")] = (r, 150.0)  # oldest (earliest expiry)
        svc._cache[("z1", "c", "d")] = (r, 200.0)

        # New lookup should evict oldest and add new entry
        svc._lookup_constraint = AsyncMock(return_value=r)

        await svc.check_constraint("x", "y", "z1")
        assert ("z1", "x", "y") in svc._cache  # new entry cached
        assert ("z1", "a", "b") not in svc._cache  # oldest evicted
        assert ("z1", "c", "d") in svc._cache  # newer kept


class TestReverseInvalidation:
    def test_invalidate_removes_both_directions(self) -> None:
        svc = _make_service()
        r = ConstraintCheckResult(allowed=True)
        svc._cache[("z1", "a", "b")] = (r, 999.0)
        svc._cache[("z1", "b", "a")] = (r, 999.0)

        svc._invalidate("z1", "a", "b")

        assert ("z1", "a", "b") not in svc._cache
        assert ("z1", "b", "a") not in svc._cache


class TestPeriodicSweep:
    @patch("nexus.bricks.governance.governance_graph_service.time")
    async def test_sweep_removes_expired_entries(self, mock_time: MagicMock) -> None:
        """After 100 lookups, expired entries should be swept."""
        svc = _make_service(cache_ttl=10.0)

        mock_time.monotonic.return_value = 100.0

        # Add some expired and some valid entries
        r = ConstraintCheckResult(allowed=True)
        svc._cache[("z1", "expired1", "b")] = (r, 50.0)  # expired
        svc._cache[("z1", "expired2", "c")] = (r, 90.0)  # expired
        svc._cache[("z1", "valid", "d")] = (r, 200.0)  # still valid

        # Set lookup count just below sweep threshold
        svc._lookup_count = 99

        svc._lookup_constraint = AsyncMock(return_value=r)

        # This lookup triggers the sweep (count reaches 100)
        await svc.check_constraint("new", "agent", "z1")

        assert ("z1", "expired1", "b") not in svc._cache
        assert ("z1", "expired2", "c") not in svc._cache
        assert ("z1", "valid", "d") in svc._cache
        assert svc._lookup_count == 0  # Reset after sweep
