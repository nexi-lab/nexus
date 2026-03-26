"""Tests for ConnectorSyncLoop metastore-first model (Issue #3266, Decision #9A).

TDD tests covering: lifecycle, capability filtering, delta→metastore writes,
full sync fallback, structured error tracking, normalization, and search notification.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.backends.connectors.cli.sync_loop import ConnectorSyncLoop
from nexus.backends.connectors.cli.sync_types import DeltaItem, DeltaSyncResult, MountSyncState

# ============================================================================
# Helpers / Fixtures
# ============================================================================


def _make_backend(
    name: str = "gmail",
    capabilities: frozenset | None = None,
    sync_delta_result: Any = None,
    has_sync_delta: bool = False,
    has_caching: bool = False,
) -> MagicMock:
    """Create a mock backend with configurable capabilities and sync_delta."""
    backend = MagicMock()
    backend.name = name

    if capabilities is None:
        from nexus.contracts.capabilities import ConnectorCapability

        capabilities = frozenset({ConnectorCapability.SYNC_ELIGIBLE})
    backend.capabilities = capabilities
    backend.has_capability = MagicMock(side_effect=lambda c: c in capabilities)

    if has_sync_delta:
        backend.sync_delta = MagicMock(return_value=sync_delta_result)
    else:
        # Remove sync_delta entirely so hasattr returns False
        del backend.sync_delta

    backend._has_caching = MagicMock(return_value=has_caching)
    backend.use_metadata_listing = True

    return backend


def _make_route(backend: Any) -> SimpleNamespace:
    return SimpleNamespace(backend=backend, mount_point="/mnt/test")


def _make_mount_service(mounts: list[dict] | None = None) -> MagicMock:
    svc = MagicMock()
    svc.list_mounts = AsyncMock(return_value=mounts or [])
    svc.sync_mount = AsyncMock(return_value={"files_scanned": 10})
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
# Lifecycle tests
# ============================================================================


class TestSyncLoopLifecycle:
    @pytest.mark.asyncio
    async def test_start_and_stop(self) -> None:
        mount_svc = _make_mount_service()
        router = _make_router()
        loop = ConnectorSyncLoop(mount_svc, router, interval=0.1)

        await loop.start()
        assert loop._running is True
        assert loop._task is not None

        await loop.stop()
        assert loop._running is False

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self) -> None:
        mount_svc = _make_mount_service()
        router = _make_router()
        loop = ConnectorSyncLoop(mount_svc, router, interval=60)

        await loop.start()
        task1 = loop._task
        await loop.start()  # Should be a no-op
        assert loop._task is task1

        await loop.stop()

    @pytest.mark.asyncio
    async def test_stop_when_not_started(self) -> None:
        mount_svc = _make_mount_service()
        router = _make_router()
        loop = ConnectorSyncLoop(mount_svc, router, interval=60)
        await loop.stop()  # Should not raise


# ============================================================================
# Capability filtering tests (Decision #5A)
# ============================================================================


class TestCapabilityFiltering:
    @pytest.mark.asyncio
    async def test_skips_non_sync_eligible(self) -> None:
        """Connectors without SYNC_ELIGIBLE are skipped."""
        backend = _make_backend(capabilities=frozenset())  # No SYNC_ELIGIBLE
        route = _make_route(backend)

        mount_svc = _make_mount_service([{"mount_point": "/mnt/test"}])
        router = _make_router({"/mnt/test": route})
        loop = ConnectorSyncLoop(mount_svc, router, interval=60)

        await loop._sync_all()

        # sync_mount should NOT be called since backend is not sync eligible
        mount_svc.sync_mount.assert_not_called()

    @pytest.mark.asyncio
    async def test_syncs_sync_eligible(self) -> None:
        """Connectors with SYNC_ELIGIBLE get synced."""
        from nexus.contracts.capabilities import ConnectorCapability

        backend = _make_backend(
            capabilities=frozenset({ConnectorCapability.SYNC_ELIGIBLE}),
        )
        route = _make_route(backend)

        mount_svc = _make_mount_service([{"mount_point": "/mnt/test"}])
        router = _make_router({"/mnt/test": route})
        loop = ConnectorSyncLoop(mount_svc, router, interval=60)

        await loop._sync_all()

        # Full sync should be called (no sync_delta on backend)
        mount_svc.sync_mount.assert_called_once()
        call_kwargs = mount_svc.sync_mount.call_args.kwargs
        assert call_kwargs["mount_point"] == "/mnt/test"
        assert call_kwargs["recursive"] is True
        assert call_kwargs["context"] is not None  # Auth context passed

    @pytest.mark.asyncio
    async def test_skips_root_mount(self) -> None:
        """Root mount '/' is always skipped."""
        mount_svc = _make_mount_service([{"mount_point": "/"}])
        router = _make_router()
        loop = ConnectorSyncLoop(mount_svc, router, interval=60)

        await loop._sync_all()

        mount_svc.sync_mount.assert_not_called()


# ============================================================================
# Delta sync normalization tests (Decision #11A)
# ============================================================================


class TestDeltaNormalization:
    def test_normalize_delta_sync_result_passthrough(self) -> None:
        """DeltaSyncResult instances pass through unchanged."""
        mount_svc = _make_mount_service()
        router = _make_router()
        loop = ConnectorSyncLoop(mount_svc, router, interval=60)

        original = DeltaSyncResult(
            added=[DeltaItem(id="msg1", path="INBOX/t-msg1.yaml")],
            sync_token="token1",
        )
        result = loop._normalize_delta(original)
        assert result is original

    def test_normalize_legacy_dict(self) -> None:
        """Legacy dict format is normalized to DeltaSyncResult."""
        mount_svc = _make_mount_service()
        router = _make_router()
        loop = ConnectorSyncLoop(mount_svc, router, interval=60)

        raw = {
            "added": [{"id": "msg1", "path": "INBOX/t-msg1.yaml", "size": 100}],
            "deleted": ["INBOX/old.yaml"],
            "history_id": "12345",
        }
        result = loop._normalize_delta(raw)
        assert isinstance(result, DeltaSyncResult)
        assert len(result.added) == 1
        assert result.added[0].id == "msg1"
        assert result.added[0].path == "INBOX/t-msg1.yaml"
        assert result.added[0].size == 100
        assert result.deleted == ["INBOX/old.yaml"]
        assert result.sync_token == "12345"

    def test_normalize_legacy_bare_string_ids(self) -> None:
        """Legacy format with bare string IDs."""
        mount_svc = _make_mount_service()
        router = _make_router()
        loop = ConnectorSyncLoop(mount_svc, router, interval=60)

        raw = {"added": ["msg1", "msg2"], "deleted": [], "history_id": "99"}
        result = loop._normalize_delta(raw)
        assert len(result.added) == 2
        assert result.added[0].id == "msg1"
        assert result.added[0].path == ""  # Bare ID, no path

    def test_normalize_full_sync_flag(self) -> None:
        mount_svc = _make_mount_service()
        router = _make_router()
        loop = ConnectorSyncLoop(mount_svc, router, interval=60)

        raw = {"added": [], "deleted": [], "full_sync": True}
        result = loop._normalize_delta(raw)
        assert result.full_sync_required is True

    def test_normalize_non_dict_returns_full_sync(self) -> None:
        """Non-dict/non-DeltaSyncResult triggers full sync."""
        mount_svc = _make_mount_service()
        router = _make_router()
        loop = ConnectorSyncLoop(mount_svc, router, interval=60)

        result = loop._normalize_delta(None)
        assert result.full_sync_required is True

        result = loop._normalize_delta("invalid")
        assert result.full_sync_required is True

    def test_normalize_empty_dict(self) -> None:
        mount_svc = _make_mount_service()
        router = _make_router()
        loop = ConnectorSyncLoop(mount_svc, router, interval=60)

        result = loop._normalize_delta({})
        assert not result.has_changes
        assert not result.full_sync_required


# ============================================================================
# Delta sync → metastore write tests (Decisions #2A, #13A, #14A)
# ============================================================================


class TestDeltaSyncToMetastore:
    @pytest.mark.asyncio
    async def test_delta_with_no_changes(self) -> None:
        """No-change delta records success without writes."""
        from nexus.contracts.capabilities import ConnectorCapability

        backend = _make_backend(
            capabilities=frozenset({ConnectorCapability.SYNC_ELIGIBLE}),
            has_sync_delta=True,
            sync_delta_result={"added": [], "deleted": [], "history_id": "100"},
        )
        route = _make_route(backend)

        mount_svc = _make_mount_service([{"mount_point": "/mnt/gmail"}])
        router = _make_router({"/mnt/gmail": route})
        loop = ConnectorSyncLoop(mount_svc, router, interval=60)

        await loop._sync_all()

        # Full sync should NOT be called (delta handled it)
        mount_svc.sync_mount.assert_not_called()

        # State should record success
        state = loop.get_mount_state("/mnt/gmail")
        assert state is not None
        assert state.is_healthy
        assert state.sync_token == "100"

    @pytest.mark.asyncio
    async def test_delta_full_sync_required_falls_back(self) -> None:
        """Delta with full_sync_required=True triggers full BFS sync."""
        from nexus.contracts.capabilities import ConnectorCapability

        backend = _make_backend(
            capabilities=frozenset({ConnectorCapability.SYNC_ELIGIBLE}),
            has_sync_delta=True,
            sync_delta_result={"added": [], "deleted": [], "full_sync": True},
        )
        route = _make_route(backend)

        mount_svc = _make_mount_service([{"mount_point": "/mnt/gmail"}])
        router = _make_router({"/mnt/gmail": route})
        loop = ConnectorSyncLoop(mount_svc, router, interval=60)

        await loop._sync_all()

        # Full sync should be called as fallback
        mount_svc.sync_mount.assert_called_once()
        assert mount_svc.sync_mount.call_args.kwargs["mount_point"] == "/mnt/gmail"


# ============================================================================
# Error handling and health tracking tests (Decision #8A)
# ============================================================================


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_sync_failure_records_error(self) -> None:
        """Failed sync records failure in mount state."""
        from nexus.contracts.capabilities import ConnectorCapability

        backend = _make_backend(
            capabilities=frozenset({ConnectorCapability.SYNC_ELIGIBLE}),
        )
        route = _make_route(backend)

        mount_svc = _make_mount_service([{"mount_point": "/mnt/gmail"}])
        mount_svc.sync_mount = AsyncMock(side_effect=RuntimeError("DB down"))
        router = _make_router({"/mnt/gmail": route})
        loop = ConnectorSyncLoop(mount_svc, router, interval=60)

        await loop._sync_all()

        state = loop.get_mount_state("/mnt/gmail")
        assert state is not None
        assert state.consecutive_failures == 1
        assert "DB down" in state.last_error

    @pytest.mark.asyncio
    async def test_delta_failure_falls_back_to_full_sync(self) -> None:
        """Failed delta sync falls back to full BFS sync."""
        from nexus.contracts.capabilities import ConnectorCapability

        backend = _make_backend(
            capabilities=frozenset({ConnectorCapability.SYNC_ELIGIBLE}),
            has_sync_delta=True,
            sync_delta_result=None,  # Will be overridden
        )
        backend.sync_delta = MagicMock(side_effect=RuntimeError("API error"))
        route = _make_route(backend)

        mount_svc = _make_mount_service([{"mount_point": "/mnt/gmail"}])
        router = _make_router({"/mnt/gmail": route})
        loop = ConnectorSyncLoop(mount_svc, router, interval=60)

        await loop._sync_all()

        # Full sync should be called as fallback
        mount_svc.sync_mount.assert_called_once()
        assert mount_svc.sync_mount.call_args.kwargs["mount_point"] == "/mnt/gmail"

    @pytest.mark.asyncio
    async def test_health_endpoint(self) -> None:
        """get_sync_health returns all mount states."""
        mount_svc = _make_mount_service()
        router = _make_router()
        loop = ConnectorSyncLoop(mount_svc, router, interval=60)

        loop._mount_states["/mnt/gmail"] = MountSyncState(mount_point="/mnt/gmail")
        loop._mount_states["/mnt/gmail"].record_success(files_synced=10)

        health = loop.get_sync_health()
        assert "/mnt/gmail" in health
        assert health["/mnt/gmail"]["total_files_synced"] == 10
        assert health["/mnt/gmail"]["is_healthy"] is True

    @pytest.mark.asyncio
    async def test_skip_mount_with_sync_in_progress(self) -> None:
        """Mount with sync_in_progress is skipped."""
        from nexus.contracts.capabilities import ConnectorCapability

        backend = _make_backend(
            capabilities=frozenset({ConnectorCapability.SYNC_ELIGIBLE}),
        )
        route = _make_route(backend)

        mount_svc = _make_mount_service([{"mount_point": "/mnt/gmail"}])
        router = _make_router({"/mnt/gmail": route})
        loop = ConnectorSyncLoop(mount_svc, router, interval=60)

        # Pre-set sync in progress
        loop._mount_states["/mnt/gmail"] = MountSyncState(mount_point="/mnt/gmail")
        loop._mount_states["/mnt/gmail"].sync_in_progress = True

        await loop._sync_all()

        mount_svc.sync_mount.assert_not_called()


# ============================================================================
# Search notification tests (Decision #6A)
# ============================================================================


class TestSearchNotification:
    @pytest.mark.asyncio
    async def test_notify_uses_full_paths(self) -> None:
        """Search notifications use full display paths, not hardcoded INBOX."""
        mount_svc = _make_mount_service()
        search_daemon = MagicMock()
        search_daemon.notify_file_change = AsyncMock()
        search_svc = MagicMock()
        search_svc._search_daemon = search_daemon
        mount_svc._search_service = search_svc

        router = _make_router()
        loop = ConnectorSyncLoop(mount_svc, router, interval=60)

        items = [
            DeltaItem(id="msg1", path="SENT/t1-msg1.yaml"),
            DeltaItem(id="evt1", path="primary/evt1.yaml"),
        ]
        await loop._notify_new_files("/mnt/test", items)

        calls = search_daemon.notify_file_change.call_args_list
        assert len(calls) == 2
        assert calls[0].args[0] == "/mnt/test/SENT/t1-msg1.yaml"
        assert calls[1].args[0] == "/mnt/test/primary/evt1.yaml"

    @pytest.mark.asyncio
    async def test_notify_skips_when_no_search_daemon(self) -> None:
        """No error when search daemon is unavailable."""
        mount_svc = _make_mount_service()
        mount_svc._search_service = None
        router = _make_router()
        loop = ConnectorSyncLoop(mount_svc, router, interval=60)

        items = [DeltaItem(id="msg1", path="INBOX/msg1.yaml")]
        await loop._notify_new_files("/mnt/test", items)  # Should not raise
