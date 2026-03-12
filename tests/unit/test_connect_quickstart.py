"""Regression tests for the documented local quickstart path."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import nexus
from nexus.raft import zone_manager


def test_local_connect_falls_back_when_full_federation_build_is_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A source checkout should still support the local SDK quickstart."""

    def _raise_missing_full_build(*args, **kwargs):
        raise RuntimeError(
            "ZoneManager requires PyO3 build with --features full. "
            "Build with: maturin develop -m rust/nexus_raft/Cargo.toml --features full"
        )

    monkeypatch.setattr(zone_manager, "ZoneManager", _raise_missing_full_build)

    nx = nexus.connect(
        config={
            "profile": "minimal",
            "data_dir": str(tmp_path / "nexus-data"),
        }
    )
    try:
        nx.sys_write("/hello.txt", b"hello")
        assert nx.sys_read("/hello.txt") == b"hello"
    finally:
        nx.close()


def test_remote_connect_skips_mount_persistence_and_parser_autodiscovery(
    monkeypatch,
) -> None:
    """Remote clients should avoid local parser bootstrap and mount writes."""
    from nexus.bricks.parsers.providers.registry import ProviderRegistry
    from nexus.bricks.parsers.registry import ParserRegistry
    from nexus.storage.remote_metastore import RemoteMetastore

    def _unexpected(*args, **kwargs):
        raise AssertionError("remote connect should not perform this bootstrap step")

    monkeypatch.setattr(RemoteMetastore, "put", _unexpected)
    monkeypatch.setattr(ParserRegistry, "register", _unexpected)
    monkeypatch.setattr(ProviderRegistry, "auto_discover", _unexpected)

    nx = nexus.connect(
        config={
            "profile": "remote",
            "url": "http://127.0.0.1:2027",
        }
    )
    try:
        assert nx.parser_registry.get_parsers() == []
        assert nx.provider_registry.get_all_providers() == []
    finally:
        nx.close()


def test_remote_connect_closes_shared_rpc_transport() -> None:
    """Remote quickstart should close the shared gRPC transport on nx.close()."""
    mock_channel = MagicMock()

    with (
        patch("nexus.remote.rpc_transport.grpc.insecure_channel", return_value=mock_channel),
        patch(
            "nexus.remote.rpc_transport.vfs_pb2_grpc.NexusVFSServiceStub", return_value=MagicMock()
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

    mock_channel.close.assert_called_once()
