"""Regression tests for the documented local quickstart path."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import grpc
import pytest

import nexus
from nexus.grpc import initialize_pb2


def test_local_connect_source_checkout_quickstart(
    tmp_path: Path,
) -> None:
    """A source checkout should still support the local SDK quickstart."""

    nx = nexus.connect(
        config={
            "profile": "embedded",
            "data_dir": str(tmp_path / "nexus-data"),
        }
    )
    try:
        nx.write("/hello.txt", b"hello")
        assert nx.sys_read("/hello.txt") == b"hello"
    finally:
        nx.close()


def test_remote_connect_skips_mount_persistence_and_parser_autodiscovery(
    monkeypatch,
) -> None:
    """Remote clients should avoid local parser bootstrap and mount writes.

    parser_registry / provider_registry are brick-layer — the kernel must
    NOT hold references to them, and the remote connect path must NOT import
    bricks.parsers at all.

    Phase 4: REMOTE profile now uses Rust RemoteBackend/RemoteMetastore
    installed via sys_setattr(backend_type="remote"). Verify the NexusFS
    holds a Rust ``PyKernel`` (not a Python RemoteMetastore) and has no
    brick-layer registries.
    """
    mock_channel = MagicMock()
    mock_stub = MagicMock()
    rpc_error = grpc.RpcError()
    rpc_error.code = lambda: grpc.StatusCode.UNIMPLEMENTED
    rpc_error.details = lambda: "Method not found"
    mock_stub.Initialize.side_effect = rpc_error

    with (
        patch("nexus.remote.rpc_transport.grpc.insecure_channel", return_value=mock_channel),
        patch(
            "nexus.remote.rpc_transport.vfs_pb2_grpc.NexusVFSServiceStub",
            return_value=mock_stub,
        ),
    ):
        nx = nexus.connect(
            config={
                "profile": "remote",
                "url": "http://127.0.0.1:2027",
            }
        )
        try:
            # Kernel must NOT hold brick-layer parser/provider registry references
            assert not hasattr(nx, "parser_registry")
            assert not hasattr(nx, "provider_registry")
            # Kernel handle must be a Rust ``PyKernel``.
            assert nx._kernel is not None
            assert nx._kernel.__class__.__name__ == "PyKernel"
            assert nx.capabilities is None
            mock_stub.Initialize.assert_called_once()
        finally:
            nx.close()


def test_remote_connect_attaches_discovered_capabilities() -> None:
    """Remote quickstart should expose Initialize capabilities on NexusFS."""
    mock_channel = MagicMock()
    mock_stub = MagicMock()
    response = initialize_pb2.InitializeResponse(
        server_name="nexus",
        server_version="test",
        protocol_version="0.1.0",
    )
    response.capabilities.posix.read = True
    response.capabilities.commands.grep.supported = True
    mock_stub.Initialize.return_value = response

    with (
        patch("nexus.remote.rpc_transport.grpc.insecure_channel", return_value=mock_channel),
        patch(
            "nexus.remote.rpc_transport.vfs_pb2_grpc.NexusVFSServiceStub",
            return_value=mock_stub,
        ),
    ):
        nx = nexus.connect(
            config={
                "profile": "remote",
                "url": "http://127.0.0.1:2027",
            }
        )
        try:
            assert nx.capabilities["posix"]["read"] is True
            assert nx.capabilities["commands"]["grep"]["supported"] is True
            mock_stub.Initialize.assert_called_once()
        finally:
            nx.close()


def test_remote_connect_closes_shared_rpc_transport() -> None:
    """Remote quickstart should close the shared gRPC transport on nx.close()."""
    mock_channel = MagicMock()
    mock_stub = MagicMock()
    rpc_error = grpc.RpcError()
    rpc_error.code = lambda: grpc.StatusCode.UNIMPLEMENTED
    rpc_error.details = lambda: "Method not found"
    mock_stub.Initialize.side_effect = rpc_error

    with (
        patch("nexus.remote.rpc_transport.grpc.insecure_channel", return_value=mock_channel),
        patch(
            "nexus.remote.rpc_transport.vfs_pb2_grpc.NexusVFSServiceStub",
            return_value=mock_stub,
        ),
        patch("nexus.security.tls.config.ZoneTlsConfig.from_env", return_value=None),
    ):
        nx = nexus.connect(
            config={
                "profile": "remote",
                "url": "http://127.0.0.1:2027",
            }
        )
        nx.close()

    mock_stub.Initialize.assert_called_once()
    mock_channel.close.assert_called_once()


def test_remote_connect_preserves_initialize_failure_when_close_fails() -> None:
    """Connection cleanup should not mask the Initialize error."""
    mock_channel = MagicMock()
    mock_channel.close.side_effect = RuntimeError("close failed")
    mock_stub = MagicMock()
    mock_stub.Initialize.side_effect = ValueError("initialize failed")

    with (
        patch("nexus.remote.rpc_transport.grpc.insecure_channel", return_value=mock_channel),
        patch(
            "nexus.remote.rpc_transport.vfs_pb2_grpc.NexusVFSServiceStub",
            return_value=mock_stub,
        ),
        pytest.raises(ValueError, match="initialize failed"),
    ):
        nexus.connect(
            config={
                "profile": "remote",
                "url": "http://127.0.0.1:2027",
            }
        )

    mock_stub.Initialize.assert_called_once()
    mock_channel.close.assert_called_once()
