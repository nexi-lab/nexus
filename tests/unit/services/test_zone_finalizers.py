"""Per-finalizer unit tests (Issue #2061).

Tests each concrete zone finalizer in isolation:
- CacheZoneFinalizer: L1 + L2 cache delegation
- SearchZoneFinalizer: bulk entity/relationship deletion
- MountZoneFinalizer: mount iteration + removal
- ReBACZoneFinalizer: bulk tuple deletion
"""

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.system_services.lifecycle.zone_finalizers.brick_drain_finalizer import (
    BrickDrainFinalizer,
)
from nexus.system_services.lifecycle.zone_finalizers.cache_finalizer import CacheZoneFinalizer
from nexus.system_services.lifecycle.zone_finalizers.mount_finalizer import MountZoneFinalizer
from nexus.system_services.lifecycle.zone_finalizers.rebac_finalizer import ReBACZoneFinalizer
from nexus.system_services.lifecycle.zone_finalizers.search_finalizer import SearchZoneFinalizer

# ---------------------------------------------------------------------------
# CacheZoneFinalizer
# ---------------------------------------------------------------------------


class TestCacheZoneFinalizer:
    def test_finalizer_key(self):
        f = CacheZoneFinalizer(file_cache=MagicMock())
        assert f.finalizer_key == "nexus.core/cache"

    @pytest.mark.asyncio
    async def test_delegates_to_file_cache_delete_zone(self):
        file_cache = MagicMock()
        file_cache.delete_zone.return_value = 42
        f = CacheZoneFinalizer(file_cache=file_cache)

        await f.finalize_zone("zone-1")

        file_cache.delete_zone.assert_called_once_with("zone-1")

    @pytest.mark.asyncio
    async def test_l2_cache_cleanup(self):
        file_cache = MagicMock()
        file_cache.delete_zone.return_value = 0
        l2_cache = AsyncMock()
        l2_cache.delete_by_pattern.return_value = 10
        f = CacheZoneFinalizer(file_cache=file_cache, l2_cache=l2_cache)

        await f.finalize_zone("zone-1")

        l2_cache.delete_by_pattern.assert_awaited_once_with("zone:zone-1:*")

    @pytest.mark.asyncio
    async def test_no_l2_cache(self):
        """When L2 cache is None, no error."""
        file_cache = MagicMock()
        file_cache.delete_zone.return_value = 0
        f = CacheZoneFinalizer(file_cache=file_cache, l2_cache=None)

        await f.finalize_zone("zone-1")  # Should not raise

    @pytest.mark.asyncio
    async def test_empty_zone_no_error(self):
        """Deleting from empty zone: zero entries, no error."""
        file_cache = MagicMock()
        file_cache.delete_zone.return_value = 0
        f = CacheZoneFinalizer(file_cache=file_cache)

        await f.finalize_zone("empty-zone")
        file_cache.delete_zone.assert_called_once_with("empty-zone")


# ---------------------------------------------------------------------------
# SearchZoneFinalizer
# ---------------------------------------------------------------------------


class TestSearchZoneFinalizer:
    def test_finalizer_key(self):
        f = SearchZoneFinalizer(session_factory=MagicMock())
        assert f.finalizer_key == "nexus.core/search"

    @pytest.mark.asyncio
    async def test_bulk_deletes_entities_and_relationships(self):
        mock_session = MagicMock()
        mock_result_entities = MagicMock()
        mock_result_entities.rowcount = 5
        mock_result_rels = MagicMock()
        mock_result_rels.rowcount = 3
        mock_session.execute.side_effect = [mock_result_entities, mock_result_rels]

        @contextmanager
        def factory():
            yield mock_session

        f = SearchZoneFinalizer(session_factory=factory)

        await f.finalize_zone("zone-1")

        assert mock_session.execute.call_count == 2
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_zone_no_error(self):
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.rowcount = 0
        mock_session.execute.return_value = mock_result

        @contextmanager
        def factory():
            yield mock_session

        f = SearchZoneFinalizer(session_factory=factory)
        await f.finalize_zone("empty-zone")  # Should not raise


# ---------------------------------------------------------------------------
# MountZoneFinalizer
# ---------------------------------------------------------------------------


class TestMountZoneFinalizer:
    def test_finalizer_key(self):
        f = MountZoneFinalizer(mount_service=MagicMock())
        assert f.finalizer_key == "nexus.core/mount"

    @pytest.mark.asyncio
    async def test_removes_zone_mounts_by_path_prefix(self):
        mount_svc = MagicMock()
        mount_svc.list_mounts.return_value = [
            {"mount_point": "/zone-1/data"},
            {"mount_point": "/zone-2/data"},
        ]
        f = MountZoneFinalizer(mount_service=mount_svc)

        await f.finalize_zone("zone-1")

        mount_svc.remove_mount.assert_called_once_with("/zone-1/data")

    @pytest.mark.asyncio
    async def test_no_mounts_for_zone(self):
        mount_svc = MagicMock()
        mount_svc.list_mounts.return_value = [
            {"mount_point": "/other/data"},
        ]
        f = MountZoneFinalizer(mount_service=mount_svc)

        await f.finalize_zone("zone-1")  # No mounts to remove

        mount_svc.remove_mount.assert_not_called()

    @pytest.mark.asyncio
    async def test_mount_removal_failure_raises(self):
        mount_svc = MagicMock()
        mount_svc.list_mounts.return_value = [
            {"mount_point": "/zone-1/data"},
        ]
        mount_svc.remove_mount.side_effect = RuntimeError("mount busy")
        f = MountZoneFinalizer(mount_service=mount_svc)

        with pytest.raises(RuntimeError, match="mount busy"):
            await f.finalize_zone("zone-1")

    @pytest.mark.asyncio
    async def test_multiple_zone_mounts(self):
        """Multiple mounts for same zone all get removed."""
        mount_svc = MagicMock()
        mount_svc.list_mounts.return_value = [
            {"mount_point": "/zone-1/uploads"},
            {"mount_point": "/zone-1/cache"},
            {"mount_point": "/zone-2/uploads"},
        ]
        f = MountZoneFinalizer(mount_service=mount_svc)

        await f.finalize_zone("zone-1")

        assert mount_svc.remove_mount.call_count == 2


# ---------------------------------------------------------------------------
# ReBACZoneFinalizer
# ---------------------------------------------------------------------------


class TestReBACZoneFinalizer:
    def test_finalizer_key(self):
        f = ReBACZoneFinalizer(session_factory=MagicMock())
        assert f.finalizer_key == "nexus.core/rebac"

    @pytest.mark.asyncio
    async def test_bulk_deletes_tuples(self):
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.rowcount = 100
        mock_session.execute.return_value = mock_result

        @contextmanager
        def factory():
            yield mock_session

        f = ReBACZoneFinalizer(session_factory=factory)

        await f.finalize_zone("zone-1")

        mock_session.execute.assert_called_once()
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_zone_no_error(self):
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.rowcount = 0
        mock_session.execute.return_value = mock_result

        @contextmanager
        def factory():
            yield mock_session

        f = ReBACZoneFinalizer(session_factory=factory)
        await f.finalize_zone("empty-zone")  # Should not raise


# ---------------------------------------------------------------------------
# BrickDrainFinalizer (#10A — Issue #2070)
# ---------------------------------------------------------------------------


class TestBrickDrainFinalizer:
    def test_finalizer_key(self):
        blm = MagicMock()
        f = BrickDrainFinalizer(brick_lifecycle_manager=blm)
        assert f.finalizer_key == "nexus.core/brick-drain"

    @pytest.mark.asyncio
    async def test_delegates_to_blm_deprovision(self):
        """BrickDrainFinalizer delegates to BrickLifecycleManager.deprovision_zone()."""
        blm = MagicMock()
        report = MagicMock()
        report.bricks_drained = 3
        report.bricks_finalized = 3
        report.drain_errors = 0
        report.finalize_errors = 0
        blm.deprovision_zone = AsyncMock(return_value=report)

        f = BrickDrainFinalizer(brick_lifecycle_manager=blm)
        await f.finalize_zone("zone-1")

        blm.deprovision_zone.assert_awaited_once_with("zone-1")

    @pytest.mark.asyncio
    async def test_raises_on_drain_errors(self):
        """BrickDrainFinalizer raises RuntimeError when drain has errors."""
        blm = MagicMock()
        report = MagicMock()
        report.bricks_drained = 2
        report.bricks_finalized = 1
        report.drain_errors = 1
        report.finalize_errors = 0
        blm.deprovision_zone = AsyncMock(return_value=report)

        f = BrickDrainFinalizer(brick_lifecycle_manager=blm)
        with pytest.raises(RuntimeError, match="1 error"):
            await f.finalize_zone("zone-1")

    @pytest.mark.asyncio
    async def test_raises_on_finalize_errors(self):
        """BrickDrainFinalizer raises RuntimeError when finalize has errors."""
        blm = MagicMock()
        report = MagicMock()
        report.bricks_drained = 2
        report.bricks_finalized = 0
        report.drain_errors = 0
        report.finalize_errors = 2
        blm.deprovision_zone = AsyncMock(return_value=report)

        f = BrickDrainFinalizer(brick_lifecycle_manager=blm)
        with pytest.raises(RuntimeError, match="2 error"):
            await f.finalize_zone("zone-1")

    @pytest.mark.asyncio
    async def test_no_errors_no_raise(self):
        """BrickDrainFinalizer does not raise when drain/finalize succeed."""
        blm = MagicMock()
        report = MagicMock()
        report.bricks_drained = 0
        report.bricks_finalized = 0
        report.drain_errors = 0
        report.finalize_errors = 0
        blm.deprovision_zone = AsyncMock(return_value=report)

        f = BrickDrainFinalizer(brick_lifecycle_manager=blm)
        await f.finalize_zone("empty-zone")  # Should not raise
