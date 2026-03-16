"""Unit tests for EdgeSyncManager — reconnection state machine.

Issue #1707: Edge split-brain resilience.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.proxy.edge_sync import EdgeSyncManager, SyncState


def _make_deps() -> dict:
    """Create mock dependencies for EdgeSyncManager."""
    queue = AsyncMock()
    queue.initialize = AsyncMock()
    queue.pending_count = AsyncMock(return_value=5)
    queue.close = AsyncMock()

    transport = AsyncMock()
    transport.call = AsyncMock(return_value={"status": "ok"})
    transport.close = AsyncMock()

    circuit = MagicMock()
    circuit.is_open = False
    circuit.record_success = AsyncMock()

    auth_manager = MagicMock()
    auth_manager.is_offline = False
    auth_manager.needs_refresh = True
    auth_manager.enter_offline_mode = MagicMock()
    auth_manager.exit_offline_mode = MagicMock()
    auth_manager.force_refresh = AsyncMock(return_value=None)

    conflict_detector = MagicMock()

    return {
        "queue": queue,
        "transport": transport,
        "circuit": circuit,
        "auth_manager": auth_manager,
        "conflict_detector": conflict_detector,
    }


class TestEdgeSyncManagerLifecycle:
    """Start/stop lifecycle tests."""

    @pytest.mark.asyncio
    async def test_starts_online(self) -> None:
        deps = _make_deps()
        mgr = EdgeSyncManager(
            queue=deps["queue"],
            transport=deps["transport"],
            circuit=deps["circuit"],
        )
        await mgr.start()
        assert mgr.state is SyncState.ONLINE
        await mgr.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_reconnect(self) -> None:
        deps = _make_deps()
        mgr = EdgeSyncManager(
            queue=deps["queue"],
            transport=deps["transport"],
            circuit=deps["circuit"],
        )
        await mgr.start()
        mgr.notify_disconnected()
        # Don't reconnect — just stop
        await mgr.stop()
        # Should not raise


class TestEdgeSyncManagerStateTransitions:
    """State machine transitions."""

    @pytest.mark.asyncio
    async def test_disconnected_state(self) -> None:
        deps = _make_deps()
        mgr = EdgeSyncManager(
            queue=deps["queue"],
            transport=deps["transport"],
            circuit=deps["circuit"],
            auth_manager=deps["auth_manager"],
        )
        await mgr.start()
        mgr.notify_disconnected()
        assert mgr.state is SyncState.DISCONNECTED
        deps["auth_manager"].enter_offline_mode.assert_called_once()
        await mgr.stop()

    @pytest.mark.asyncio
    async def test_reconnect_reaches_online(self) -> None:
        deps = _make_deps()
        mgr = EdgeSyncManager(
            queue=deps["queue"],
            transport=deps["transport"],
            circuit=deps["circuit"],
            auth_manager=deps["auth_manager"],
            conflict_detector=deps["conflict_detector"],
        )
        await mgr.start()

        mgr.notify_disconnected()
        assert mgr.state is SyncState.DISCONNECTED

        mgr.notify_connected()
        # Wait for reconnection to complete
        await asyncio.sleep(0.1)

        assert mgr.state is SyncState.ONLINE
        await mgr.stop()

    @pytest.mark.asyncio
    async def test_reconnect_calls_auth_refresh(self) -> None:
        deps = _make_deps()
        mgr = EdgeSyncManager(
            queue=deps["queue"],
            transport=deps["transport"],
            circuit=deps["circuit"],
            auth_manager=deps["auth_manager"],
        )
        await mgr.start()

        mgr.notify_disconnected()
        mgr.notify_connected()
        await asyncio.sleep(0.1)

        deps["auth_manager"].exit_offline_mode.assert_called_once()
        deps["auth_manager"].force_refresh.assert_called_once()
        await mgr.stop()

    @pytest.mark.asyncio
    async def test_reconnect_fails_goes_back_to_disconnected(self) -> None:
        deps = _make_deps()
        # Make health check fail
        deps["circuit"].is_open = True
        mgr = EdgeSyncManager(
            queue=deps["queue"],
            transport=deps["transport"],
            circuit=deps["circuit"],
        )
        await mgr.start()

        mgr.notify_disconnected()
        mgr.notify_connected()
        await asyncio.sleep(0.1)

        assert mgr.state is SyncState.DISCONNECTED
        await mgr.stop()


class TestEdgeSyncManagerNotifyIdempotency:
    """Multiple notify calls should be idempotent."""

    @pytest.mark.asyncio
    async def test_multiple_disconnect_calls(self) -> None:
        deps = _make_deps()
        mgr = EdgeSyncManager(
            queue=deps["queue"],
            transport=deps["transport"],
            circuit=deps["circuit"],
            auth_manager=deps["auth_manager"],
        )
        await mgr.start()

        mgr.notify_disconnected()
        mgr.notify_disconnected()
        mgr.notify_disconnected()

        # Should only call enter_offline_mode once (first transition)
        assert deps["auth_manager"].enter_offline_mode.call_count == 1
        await mgr.stop()

    @pytest.mark.asyncio
    async def test_notify_connected_while_online_is_noop(self) -> None:
        deps = _make_deps()
        mgr = EdgeSyncManager(
            queue=deps["queue"],
            transport=deps["transport"],
            circuit=deps["circuit"],
        )
        await mgr.start()
        # Should not trigger reconnection
        mgr.notify_connected()
        assert mgr.state is SyncState.ONLINE
        await mgr.stop()


class TestEdgeSyncManagerHealthCheckUrl:
    """Bug #6: health_check_url value must be used, not hardcoded."""

    async def test_custom_health_check_url_is_used(self) -> None:
        deps = _make_deps()
        mgr = EdgeSyncManager(
            queue=deps["queue"],
            transport=deps["transport"],
            circuit=deps["circuit"],
            health_check_url="custom.health.endpoint",
        )
        await mgr.start()

        mgr.notify_disconnected()
        mgr.notify_connected()
        await asyncio.sleep(0.1)

        # The transport should have been called with the custom URL, not "health.check"
        deps["transport"].call.assert_called_with("custom.health.endpoint", params={})
        await mgr.stop()

    async def test_no_health_check_url_skips_transport_call(self) -> None:
        deps = _make_deps()
        deps["circuit"].is_open = False
        mgr = EdgeSyncManager(
            queue=deps["queue"],
            transport=deps["transport"],
            circuit=deps["circuit"],
            # No health_check_url — should use circuit state
        )
        await mgr.start()

        mgr.notify_disconnected()
        mgr.notify_connected()
        await asyncio.sleep(0.1)

        # Transport.call should NOT be called for health check (circuit state used instead)
        # But it might be called for other operations — check no "health" call was made
        for call in deps["transport"].call.call_args_list:
            assert "health" not in str(call), "Should not call transport for health check"
        await mgr.stop()


class TestEdgeSyncManagerStatusDict:
    """to_status_dict() returns monitoring info."""

    @pytest.mark.asyncio
    async def test_status_dict_online(self) -> None:
        deps = _make_deps()
        mgr = EdgeSyncManager(
            queue=deps["queue"],
            transport=deps["transport"],
            circuit=deps["circuit"],
            node_id="edge-42",
        )
        await mgr.start()
        status = mgr.to_status_dict()
        assert status["node_id"] == "edge-42"
        assert status["state"] == "online"
        await mgr.stop()

    @pytest.mark.asyncio
    async def test_status_dict_disconnected(self) -> None:
        deps = _make_deps()
        auth_mgr = deps["auth_manager"]
        auth_mgr.is_offline = True
        auth_mgr.needs_refresh = False
        mgr = EdgeSyncManager(
            queue=deps["queue"],
            transport=deps["transport"],
            circuit=deps["circuit"],
            auth_manager=auth_mgr,
            node_id="edge-42",
        )
        await mgr.start()
        mgr.notify_disconnected()
        status = mgr.to_status_dict()
        assert status["state"] == "disconnected"
        assert status["auth_offline"] is True
        await mgr.stop()
