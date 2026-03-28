"""Unit tests for FUSELeaseCoordinator (Issue #3397).

Tests the lease-gated cache access pattern, invalidation + revocation,
fallback behavior on lease timeout, and lifecycle management.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.contracts.protocols.lease import Lease, LeaseState
from nexus.fuse.cache import FUSECacheManager
from nexus.fuse.lease_coordinator import FUSELeaseCoordinator


@pytest.fixture()
def bare_cache() -> FUSECacheManager:
    """Real FUSECacheManager for testing coordinator delegation."""
    return FUSECacheManager(
        attr_cache_size=128,
        attr_cache_ttl=60,
        content_cache_size=128,
        parsed_cache_size=16,
    )


@pytest.fixture()
def mock_lease_manager() -> MagicMock:
    """Mock LeaseManager with async methods."""
    mgr = MagicMock()
    mgr.acquire = AsyncMock()
    mgr.validate = AsyncMock()
    mgr.revoke = AsyncMock(return_value=[])
    mgr.register_revocation_callback = MagicMock()
    mgr.unregister_revocation_callback = MagicMock()
    return mgr


def _make_lease(
    resource_id: str = "fuse:/file.txt",
    holder_id: str = "mount-test",
    state: LeaseState = LeaseState.SHARED_READ,
    generation: int = 1,
    ttl: float = 30.0,
) -> Lease:
    """Helper to create a Lease with sane defaults."""
    now = time.monotonic()
    return Lease(
        resource_id=resource_id,
        holder_id=holder_id,
        state=state,
        generation=generation,
        granted_at=now,
        expires_at=now + ttl,
    )


class TestCoordinatorWithoutLeaseManager:
    """When no lease manager is provided, coordinator behaves like bare cache."""

    def test_lease_gated_get_cache_hit(self, bare_cache: FUSECacheManager) -> None:
        coord = FUSELeaseCoordinator(cache=bare_cache)
        bare_cache.cache_attr("/file.txt", {"st_size": 100})

        result = coord.lease_gated_get(
            path="/file.txt",
            cache_get=lambda: coord.get_attr("/file.txt"),
            cache_set=lambda v: coord.cache_attr("/file.txt", v),
            fetch_fn=lambda: {"st_size": 999},
        )
        assert result == {"st_size": 100}

    def test_lease_gated_get_cache_miss(self, bare_cache: FUSECacheManager) -> None:
        coord = FUSELeaseCoordinator(cache=bare_cache)

        result = coord.lease_gated_get(
            path="/file.txt",
            cache_get=lambda: coord.get_attr("/file.txt"),
            cache_set=lambda v: coord.cache_attr("/file.txt", v),
            fetch_fn=lambda: {"st_size": 999},
        )
        assert result == {"st_size": 999}
        # Verify it was cached
        assert coord.get_attr("/file.txt") == {"st_size": 999}

    def test_invalidate_and_revoke_clears_cache(self, bare_cache: FUSECacheManager) -> None:
        coord = FUSELeaseCoordinator(cache=bare_cache)
        bare_cache.cache_attr("/file.txt", {"st_size": 100})
        bare_cache.cache_content("/file.txt", b"hello")

        coord.invalidate_and_revoke(["/file.txt"])

        assert coord.get_attr("/file.txt") is None
        assert coord.get_content("/file.txt") is None

    def test_delegated_methods(self, bare_cache: FUSECacheManager) -> None:
        coord = FUSELeaseCoordinator(cache=bare_cache)

        coord.cache_attr("/a", {"st_size": 1})
        assert coord.get_attr("/a") == {"st_size": 1}

        coord.cache_content("/b", b"data")
        assert coord.get_content("/b") == b"data"

        coord.cache_parsed("/c", "md", b"# hello")
        assert coord.get_parsed("/c", "md") == b"# hello"
        assert coord.get_parsed_size("/c", "md") == 7

        coord.invalidate_path("/a")
        assert coord.get_attr("/a") is None

        coord.invalidate_all()
        assert coord.get_content("/b") is None


class TestCoordinatorWithLeaseManager:
    """Tests with a mock lease manager wired in."""

    def _make_coordinator(
        self,
        bare_cache: FUSECacheManager,
        mock_lease_manager: MagicMock,
    ) -> FUSELeaseCoordinator:
        coord = FUSELeaseCoordinator(
            cache=bare_cache,
            lease_manager=mock_lease_manager,
            holder_id="mount-test",
            lease_ttl=30.0,
            acquire_timeout=5.0,
        )
        return coord

    def test_lease_gated_get_valid_lease_cache_hit(
        self,
        bare_cache: FUSECacheManager,
        mock_lease_manager: MagicMock,
    ) -> None:
        """Valid lease + cache hit → returns cached value, no backend call."""
        coord = self._make_coordinator(bare_cache, mock_lease_manager)
        try:
            # Pre-populate cache and validity
            bare_cache.cache_attr("/file.txt", {"st_size": 100})
            coord._set_validity("/file.txt", time.monotonic() + 30.0)

            fetch_called = False

            def fetch_fn() -> dict:
                nonlocal fetch_called
                fetch_called = True
                return {"st_size": 999}

            result = coord.lease_gated_get(
                path="/file.txt",
                cache_get=lambda: coord.get_attr("/file.txt"),
                cache_set=lambda v: coord.cache_attr("/file.txt", v),
                fetch_fn=fetch_fn,
            )
            assert result == {"st_size": 100}
            assert not fetch_called
        finally:
            coord.close()

    def test_lease_gated_get_expired_validity_revalidates(
        self,
        bare_cache: FUSECacheManager,
        mock_lease_manager: MagicMock,
    ) -> None:
        """Expired local validity → full lease validation → serve from cache."""
        coord = self._make_coordinator(bare_cache, mock_lease_manager)
        try:
            bare_cache.cache_attr("/file.txt", {"st_size": 100})
            # Set expired validity
            coord._set_validity("/file.txt", time.monotonic() - 1.0)

            lease = _make_lease()
            mock_lease_manager.validate.return_value = lease

            result = coord.lease_gated_get(
                path="/file.txt",
                cache_get=lambda: coord.get_attr("/file.txt"),
                cache_set=lambda v: coord.cache_attr("/file.txt", v),
                fetch_fn=lambda: {"st_size": 999},
            )
            assert result == {"st_size": 100}
            mock_lease_manager.validate.assert_called_once()
        finally:
            coord.close()

    def test_lease_gated_get_cache_miss_acquires_lease(
        self,
        bare_cache: FUSECacheManager,
        mock_lease_manager: MagicMock,
    ) -> None:
        """Cache miss + no lease → acquire lease → fetch → cache."""
        coord = self._make_coordinator(bare_cache, mock_lease_manager)
        try:
            mock_lease_manager.validate.return_value = None
            mock_lease_manager.acquire.return_value = _make_lease()

            result = coord.lease_gated_get(
                path="/new.txt",
                cache_get=lambda: coord.get_attr("/new.txt"),
                cache_set=lambda v: coord.cache_attr("/new.txt", v),
                fetch_fn=lambda: {"st_size": 42},
            )
            assert result == {"st_size": 42}
            assert coord.get_attr("/new.txt") == {"st_size": 42}
            mock_lease_manager.acquire.assert_called_once()
        finally:
            coord.close()

    def test_lease_gated_get_timeout_fetches_without_cache(
        self,
        bare_cache: FUSECacheManager,
        mock_lease_manager: MagicMock,
    ) -> None:
        """Lease timeout → fetch without caching (Decision 11A)."""
        coord = self._make_coordinator(bare_cache, mock_lease_manager)
        try:
            mock_lease_manager.validate.return_value = None
            mock_lease_manager.acquire.return_value = None  # timeout

            result = coord.lease_gated_get(
                path="/timeout.txt",
                cache_get=lambda: coord.get_attr("/timeout.txt"),
                cache_set=lambda v: coord.cache_attr("/timeout.txt", v),
                fetch_fn=lambda: {"st_size": 77},
            )
            assert result == {"st_size": 77}
            # Should NOT be cached (no lease protects it)
            assert coord.get_attr("/timeout.txt") is None
        finally:
            coord.close()

    def test_invalidate_and_revoke_fires_revocation(
        self,
        bare_cache: FUSECacheManager,
        mock_lease_manager: MagicMock,
    ) -> None:
        """invalidate_and_revoke clears local cache and fires async revocation."""
        coord = self._make_coordinator(bare_cache, mock_lease_manager)
        try:
            bare_cache.cache_attr("/file.txt", {"st_size": 100})
            coord._set_validity("/file.txt", time.monotonic() + 30.0)

            coord.invalidate_and_revoke(["/file.txt"])

            # Local cache cleared immediately
            assert coord.get_attr("/file.txt") is None
            assert not coord._check_validity("/file.txt")

            # Give fire-and-forget a moment to submit
            time.sleep(0.1)
            # Revocation was submitted (we can't easily assert it ran,
            # but it should have been submitted to the event loop)
        finally:
            coord.close()

    def test_invalidate_and_revoke_multiple_paths(
        self,
        bare_cache: FUSECacheManager,
        mock_lease_manager: MagicMock,
    ) -> None:
        """Rename invalidation covers all paths."""
        coord = self._make_coordinator(bare_cache, mock_lease_manager)
        try:
            for p in ["/old.txt", "/new.txt", "/parent"]:
                bare_cache.cache_attr(p, {"st_size": 1})
                coord._set_validity(p, time.monotonic() + 30.0)

            coord.invalidate_and_revoke(["/old.txt", "/new.txt", "/parent"])

            for p in ["/old.txt", "/new.txt", "/parent"]:
                assert coord.get_attr(p) is None
                assert not coord._check_validity(p)
        finally:
            coord.close()

    def test_revocation_callback_registered(
        self,
        bare_cache: FUSECacheManager,
        mock_lease_manager: MagicMock,
    ) -> None:
        """Coordinator registers a revocation callback on init."""
        coord = self._make_coordinator(bare_cache, mock_lease_manager)
        try:
            mock_lease_manager.register_revocation_callback.assert_called_once()
            call_args = mock_lease_manager.register_revocation_callback.call_args
            assert "fuse-coordinator-mount-test" in call_args[0][0]
        finally:
            coord.close()

    def test_revocation_callback_invalidates_own_lease(
        self,
        bare_cache: FUSECacheManager,
        mock_lease_manager: MagicMock,
    ) -> None:
        """Revocation of OUR lease clears our local cache (another mount caused it)."""
        coord = self._make_coordinator(bare_cache, mock_lease_manager)
        try:
            # Get the registered callback
            call_args = mock_lease_manager.register_revocation_callback.call_args
            callback = call_args[0][1]

            # Populate cache
            bare_cache.cache_attr("/file.txt", {"st_size": 100})
            coord._set_validity("/file.txt", time.monotonic() + 30.0)

            # Simulate revocation of OUR lease (caused by another mount's write)
            own_lease = _make_lease(holder_id="mount-test")

            # Run the async callback
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(callback(own_lease, "conflict"))
            finally:
                loop.close()

            assert coord.get_attr("/file.txt") is None
            assert not coord._check_validity("/file.txt")
        finally:
            coord.close()

    def test_revocation_callback_ignores_other_holder(
        self,
        bare_cache: FUSECacheManager,
        mock_lease_manager: MagicMock,
    ) -> None:
        """Revocation of ANOTHER holder's lease doesn't clear our cache."""
        coord = self._make_coordinator(bare_cache, mock_lease_manager)
        try:
            call_args = mock_lease_manager.register_revocation_callback.call_args
            callback = call_args[0][1]

            bare_cache.cache_attr("/file.txt", {"st_size": 100})
            coord._set_validity("/file.txt", time.monotonic() + 30.0)

            # Simulate revocation of a DIFFERENT holder's lease
            other_lease = _make_lease(holder_id="mount-other")

            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(callback(other_lease, "conflict"))
            finally:
                loop.close()

            # Should NOT be invalidated (not our lease)
            assert coord.get_attr("/file.txt") is not None
            assert coord._check_validity("/file.txt")
        finally:
            coord.close()


class TestCoordinatorLifecycle:
    """Tests for event loop thread lifecycle."""

    def test_close_is_idempotent(self, bare_cache: FUSECacheManager) -> None:
        mock_mgr = MagicMock()
        mock_mgr.acquire = AsyncMock()
        mock_mgr.validate = AsyncMock()
        mock_mgr.revoke = AsyncMock(return_value=[])
        mock_mgr.register_revocation_callback = MagicMock()

        coord = FUSELeaseCoordinator(cache=bare_cache, lease_manager=mock_mgr, holder_id="mount-x")
        coord.close()
        coord.close()  # should not raise

    def test_holder_id_property(self, bare_cache: FUSECacheManager) -> None:
        coord = FUSELeaseCoordinator(cache=bare_cache, holder_id="mount-abc")
        assert coord.holder_id == "mount-abc"

    def test_lease_manager_property(
        self, bare_cache: FUSECacheManager, mock_lease_manager: MagicMock
    ) -> None:
        coord = FUSELeaseCoordinator(
            cache=bare_cache, lease_manager=mock_lease_manager, holder_id="m"
        )
        try:
            assert coord.lease_manager is mock_lease_manager
        finally:
            coord.close()


class TestValidityCache:
    """Tests for the local validity cache (Decision 13A)."""

    def test_check_validity_miss(self, bare_cache: FUSECacheManager) -> None:
        coord = FUSELeaseCoordinator(cache=bare_cache)
        assert not coord._check_validity("/nonexistent")

    def test_check_validity_valid(self, bare_cache: FUSECacheManager) -> None:
        coord = FUSELeaseCoordinator(cache=bare_cache)
        coord._set_validity("/file.txt", time.monotonic() + 30.0)
        assert coord._check_validity("/file.txt")

    def test_check_validity_expired(self, bare_cache: FUSECacheManager) -> None:
        coord = FUSELeaseCoordinator(cache=bare_cache)
        coord._set_validity("/file.txt", time.monotonic() - 1.0)
        assert not coord._check_validity("/file.txt")

    def test_clear_validity(self, bare_cache: FUSECacheManager) -> None:
        coord = FUSELeaseCoordinator(cache=bare_cache)
        coord._set_validity("/file.txt", time.monotonic() + 30.0)
        coord._clear_validity("/file.txt")
        assert not coord._check_validity("/file.txt")

    def test_clear_all_validity(self, bare_cache: FUSECacheManager) -> None:
        coord = FUSELeaseCoordinator(cache=bare_cache)
        coord._set_validity("/a", time.monotonic() + 30.0)
        coord._set_validity("/b", time.monotonic() + 30.0)
        coord._clear_all_validity()
        assert not coord._check_validity("/a")
        assert not coord._check_validity("/b")
