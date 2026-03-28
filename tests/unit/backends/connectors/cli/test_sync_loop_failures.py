"""Failure scenario tests for ConnectorSyncLoop (Issue #3266, Decision #12A).

Tests for partial delta failure, concurrent sync protection, metastore write
failures, and timeout handling. Prioritized by blast radius.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.backends.connectors.cli.sync_loop import ConnectorSyncLoop
from nexus.backends.connectors.cli.sync_types import DeltaItem


def _make_backend(backend_features: frozenset | None = None, **kwargs: Any) -> MagicMock:
    from nexus.contracts.backend_features import BackendFeature

    backend = MagicMock()
    backend.name = "test"
    backend.backend_features = backend_features or frozenset({BackendFeature.SYNC_ELIGIBLE})
    backend.has_feature = MagicMock(side_effect=lambda c: c in backend.backend_features)
    backend._has_caching = MagicMock(return_value=False)
    backend.use_metadata_listing = True
    return backend


def _make_route(backend: Any) -> SimpleNamespace:
    return SimpleNamespace(backend=backend, mount_point="/mnt/test")


def _make_mount_service(mounts: list[dict] | None = None) -> MagicMock:
    svc = MagicMock()
    svc.list_mounts = AsyncMock(return_value=mounts or [])
    svc.sync_mount = AsyncMock(return_value={"files_scanned": 0})
    svc._metastore = None
    svc._sync_service = None
    svc._search_service = None
    return svc


def _make_router(route_map: dict[str, Any] | None = None) -> MagicMock:
    router = MagicMock()

    def _route(path: str) -> Any:
        if route_map:
            for prefix, route_obj in route_map.items():
                if path.startswith(prefix):
                    return route_obj
        return None

    router.route = MagicMock(side_effect=_route)
    return router


# ============================================================================
# Partial delta failure (12.1) — highest blast radius
# ============================================================================


class TestPartialDeltaFailure:
    @pytest.mark.asyncio
    async def test_partial_content_fetch_failure_commits_successful_items(self) -> None:
        """When 3 of 5 items fail to fetch, the other 2 still get written."""
        backend = _make_backend()

        call_count = 0

        def _read_content(content_id: str, context: Any = None) -> bytes:
            nonlocal call_count
            call_count += 1
            path = context.backend_path if context else content_id
            if "fail" in path:
                raise RuntimeError("API error for this item")
            return b"content"

        backend.read_content = MagicMock(side_effect=_read_content)

        loop = ConnectorSyncLoop(_make_mount_service(), _make_router(), interval=60)
        items = [
            DeltaItem(id="ok1", path="INBOX/ok1.yaml"),
            DeltaItem(id="fail1", path="INBOX/fail1.yaml"),
            DeltaItem(id="ok2", path="INBOX/ok2.yaml"),
            DeltaItem(id="fail2", path="INBOX/fail2.yaml"),
            DeltaItem(id="fail3", path="INBOX/fail3.yaml"),
        ]

        results = loop._fetch_delta_content(backend, items)

        # Only 2 items should succeed
        assert len(results) == 2
        assert results[0][0].id == "ok1"
        assert results[1][0].id == "ok2"

    @pytest.mark.asyncio
    async def test_all_items_fail_returns_empty(self) -> None:
        """When all items fail, returns empty list (no crash)."""
        backend = _make_backend()
        backend.read_content = MagicMock(side_effect=RuntimeError("total failure"))

        loop = ConnectorSyncLoop(_make_mount_service(), _make_router(), interval=60)
        items = [DeltaItem(id=f"msg{i}", path=f"INBOX/msg{i}.yaml") for i in range(5)]

        results = loop._fetch_delta_content(backend, items)
        assert results == []


# ============================================================================
# Concurrent sync protection (12.4)
# ============================================================================


class TestConcurrentSyncProtection:
    @pytest.mark.asyncio
    async def test_concurrent_sync_for_same_mount_is_blocked(self) -> None:
        """Second sync attempt for a mount is skipped while first is running."""
        backend = _make_backend()
        del backend.sync_delta  # Force full sync path
        route = _make_route(backend)

        # Simulate a slow sync
        mount_svc = _make_mount_service([{"mount_point": "/mnt/gmail"}])

        async def _slow_sync(**kwargs: Any) -> dict:
            await asyncio.sleep(0.5)
            return {"files_scanned": 10}

        mount_svc.sync_mount = _slow_sync
        router = _make_router({"/mnt/gmail": route})
        loop = ConnectorSyncLoop(mount_svc, router, interval=60)

        # Start first sync
        task1 = asyncio.create_task(loop._sync_all())
        await asyncio.sleep(0.05)  # Let it start

        # Second sync should skip (in_progress)
        task2 = asyncio.create_task(loop._sync_all())

        await asyncio.gather(task1, task2)

        state = loop.get_mount_state("/mnt/gmail")
        assert state is not None
        # sync_in_progress should be released
        assert state.sync_in_progress is False


# ============================================================================
# Timeout handling (Decision #16B)
# ============================================================================


class TestTimeoutHandling:
    @pytest.mark.asyncio
    async def test_per_mount_timeout_records_failure(self) -> None:
        """Mount that exceeds timeout gets failure recorded."""
        backend = _make_backend()
        del backend.sync_delta
        route = _make_route(backend)

        mount_svc = _make_mount_service([{"mount_point": "/mnt/slow"}])

        async def _very_slow_sync(**kwargs: Any) -> dict:
            await asyncio.sleep(10)
            return {"files_scanned": 0}

        mount_svc.sync_mount = _very_slow_sync
        router = _make_router({"/mnt/slow": route})

        # Very short timeout for testing
        loop = ConnectorSyncLoop(mount_svc, router, interval=60)
        loop._per_mount_timeout = 0.1  # 100ms timeout

        await loop._sync_all()

        state = loop.get_mount_state("/mnt/slow")
        assert state is not None
        assert state.consecutive_failures == 1
        assert "timed out" in state.last_error
        assert state.sync_in_progress is False  # Released after timeout


# ============================================================================
# List mounts failure
# ============================================================================


class TestListMountsFailure:
    @pytest.mark.asyncio
    async def test_list_mounts_failure_does_not_crash(self) -> None:
        """If list_mounts raises, the loop continues."""
        mount_svc = _make_mount_service()
        mount_svc.list_mounts = AsyncMock(side_effect=RuntimeError("mount service down"))
        router = _make_router()
        loop = ConnectorSyncLoop(mount_svc, router, interval=60)

        await loop._sync_all()  # Should not raise


# ============================================================================
# Metastore write failure (12.2)
# ============================================================================


class TestMetastoreWriteFailure:
    @pytest.mark.asyncio
    async def test_metastore_set_failure_is_non_fatal(self) -> None:
        """If metastore.set() fails, other items still get written."""
        backend = _make_backend()
        backend.read_content = MagicMock(return_value=b"content")
        backend._has_caching = MagicMock(return_value=False)

        metastore = MagicMock()
        call_count = 0

        def _set(path: str, meta: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("metastore write error")

        metastore.set = MagicMock(side_effect=_set)

        mount_svc = _make_mount_service()
        mount_svc._metastore = metastore

        router = _make_router()
        loop = ConnectorSyncLoop(mount_svc, router, interval=60)

        items = [
            (DeltaItem(id="ok1", path="INBOX/ok1.yaml"), b"content1"),
            (DeltaItem(id="fail", path="INBOX/fail.yaml"), b"content2"),
            (DeltaItem(id="ok2", path="INBOX/ok2.yaml"), b"content3"),
        ]

        synced = await loop._batch_write_metastore("/mnt/test", backend, items)

        # All 3 items should be counted (metastore failure is non-fatal)
        # Item 1 and 3 succeed metastore write, item 2 fails but content still counted
        assert synced == 3
