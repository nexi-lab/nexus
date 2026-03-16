"""Tests for unified gRPC server lifecycle (#1249)."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_app(**state_attrs) -> MagicMock:
    """Build a mock FastAPI app with explicit state attributes."""
    app = MagicMock()
    app.state = SimpleNamespace(**state_attrs)
    return app


class TestStartupGrpc:
    """startup_grpc registers VFSServicer on the configured port."""

    @pytest.mark.anyio
    async def test_disabled_when_no_port(self) -> None:
        """No NEXUS_GRPC_PORT (or 0) -> returns empty, no server started."""
        from nexus.grpc.server import startup_grpc

        app = _make_app()
        svc = MagicMock()

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NEXUS_GRPC_PORT", None)
            result = await startup_grpc(app, svc)

        assert result == []
        assert not hasattr(app.state, "grpc_server")

    @pytest.mark.anyio
    async def test_disabled_when_no_nexus_fs(self) -> None:
        """Port set but no nexus_fs -> returns empty."""
        from nexus.grpc.server import startup_grpc

        app = _make_app(nexus_fs=None)
        svc = MagicMock()

        with patch.dict(os.environ, {"NEXUS_GRPC_PORT": "50051"}):
            result = await startup_grpc(app, svc)

        assert result == []

    @pytest.mark.anyio
    async def test_starts_server_with_vfs(self) -> None:
        """Port + nexus_fs -> server started, stored on app.state.grpc_server."""
        from nexus.grpc.server import startup_grpc

        app = _make_app(
            nexus_fs=MagicMock(),
            exposed_methods={},
            auth_provider=None,
            api_key=None,
            subscription_manager=None,
        )
        svc = MagicMock()
        svc.service_coordinator = None  # no coordinator in test

        mock_server = AsyncMock()
        with (
            patch.dict(os.environ, {"NEXUS_GRPC_PORT": "50051"}),
            patch("grpc.aio.server", return_value=mock_server),
            patch("nexus.grpc.vfs.vfs_pb2_grpc.add_NexusVFSServiceServicer_to_server"),
            patch("nexus.grpc.servicer.VFSServicer"),
        ):
            result = await startup_grpc(app, svc)

        assert result == []
        assert app.state.grpc_server is mock_server
        mock_server.add_insecure_port.assert_called_once_with("127.0.0.1:50051")
        mock_server.start.assert_awaited_once()


class TestShutdownGrpc:
    """shutdown_grpc stops the server gracefully."""

    @pytest.mark.anyio
    async def test_stops_running_server(self) -> None:
        from nexus.grpc.server import shutdown_grpc

        mock_server = AsyncMock()
        app = _make_app(grpc_server=mock_server)
        svc = MagicMock()

        await shutdown_grpc(app, svc)

        mock_server.stop.assert_awaited_once_with(grace=5)

    @pytest.mark.anyio
    async def test_noop_when_no_server(self) -> None:
        from nexus.grpc.server import shutdown_grpc

        app = _make_app()  # no grpc_server attr
        svc = MagicMock()

        await shutdown_grpc(app, svc)  # should not raise
