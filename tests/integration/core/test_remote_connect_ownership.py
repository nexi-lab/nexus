"""Remote SDK startup releases real channels and kernel subprocesses."""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest

import nexus
from nexus.core.nexus_fs import NexusFS
from nexus.remote.kernel_client import KernelClient
from nexus.remote.rpc_transport import RPCTransport


@pytest.fixture
def remote_peer(monkeypatch: pytest.MonkeyPatch) -> Iterator[KernelClient]:
    peer = KernelClient(ephemeral=True)
    peer.open()
    monkeypatch.setenv("NEXUS_GRPC_PORT", peer._server_address.rsplit(":", 1)[1])
    monkeypatch.setenv("NEXUS_GRPC_TLS", "false")
    monkeypatch.delenv("NEXUS_API_KEY", raising=False)
    try:
        yield peer
    finally:
        peer.close()


@pytest.fixture
def acquired_resources(remote_peer: KernelClient, monkeypatch: pytest.MonkeyPatch):
    kernels = []
    transports = []
    open_kernel = nexus._open_local_kernel

    def track_kernel(*args, **kwargs):
        kernel = open_kernel(*args, **kwargs)
        kernels.append((kernel, kernel._process, kernel._ephemeral_dir))
        return kernel

    def track_transport(*args, **kwargs):
        transport = RPCTransport(*args, **kwargs)
        close = Mock(wraps=transport.close)
        monkeypatch.setattr(transport, "close", close)
        transports.append((transport, close))
        return transport

    monkeypatch.setattr(nexus, "_open_local_kernel", track_kernel)
    monkeypatch.setattr("nexus.remote.rpc_transport.RPCTransport", track_transport)
    try:
        yield kernels, transports
    finally:
        # A regression must not leave the test's own children running.
        for kernel, _, _ in kernels:
            kernel.close()
        for transport, _ in transports:
            transport.close()


def remote_config(peer: KernelClient) -> dict:
    return {"profile": "remote", "url": f"grpc://{peer._server_address}", "timeout": 3}


def assert_released(resources):
    kernels, transports = resources
    for kernel, process, ephemeral_dir in kernels:
        assert process is not None and process.poll() is not None
        assert kernel._transport is None
        assert ephemeral_dir is not None and not Path(ephemeral_dir).exists()
    assert len(transports) == 1
    transports[0][1].assert_called_once()


@pytest.mark.parametrize("phase", ["kernel", "filesystem", "mount", "services", "overrides"])
def test_failed_remote_connect_releases_acquired_resources(
    remote_peer: KernelClient, acquired_resources, monkeypatch: pytest.MonkeyPatch, phase: str
):
    failure = RuntimeError(f"Failed during {phase}")

    def fail(*args, **kwargs):
        raise failure

    if phase == "kernel":
        monkeypatch.setattr(nexus, "_open_local_kernel", fail)
    elif phase == "filesystem":
        monkeypatch.setattr("nexus.core.nexus_fs.NexusFS", fail)
    elif phase == "mount":
        setattr_original = NexusFS.sys_setattr

        def fail_mount(self, path, *args, **kwargs):
            if path == "/" and kwargs.get("backend_type") == "remote":
                raise failure
            return setattr_original(self, path, *args, **kwargs)

        monkeypatch.setattr(NexusFS, "sys_setattr", fail_mount)
    else:
        import nexus.factory._remote as remote

        name = (
            "_boot_remote_services"
            if phase == "services"
            else "install_remote_kernel_rpc_overrides"
        )
        original = getattr(remote, name)

        def fail_after_wiring(*args, **kwargs):
            original(*args, **kwargs)
            raise failure

        monkeypatch.setattr(remote, name, fail_after_wiring)

    with pytest.raises(RuntimeError) as caught:
        nexus.connect(remote_config(remote_peer))
    assert caught.value is failure
    assert_released(acquired_resources)
    assert remote_peer._transport.health_check()


def test_interrupted_remote_connect_drains_despite_service_cleanup_failure(
    remote_peer: KernelClient, acquired_resources, monkeypatch: pytest.MonkeyPatch
):
    def interrupted(filesystem, **kwargs):
        def failed_cleanup():
            raise RuntimeError("Service close failed")

        monkeypatch.setattr(filesystem._kernel, "service_close_all", failed_cleanup)
        raise KeyboardInterrupt("Startup interrupted")

    monkeypatch.setattr("nexus.factory._remote._boot_remote_services", interrupted)
    with pytest.raises(KeyboardInterrupt, match="Startup interrupted"):
        nexus.connect(remote_config(remote_peer))
    assert_released(acquired_resources)
    assert remote_peer._transport.health_check()


def test_remote_connect_transfers_ownership_and_can_reconnect(
    remote_peer: KernelClient, acquired_resources
):
    kernels, transports = acquired_resources
    for attempt in range(2):
        filesystem = nexus.connect(remote_config(remote_peer))
        kernel, process, ephemeral_dir = kernels[attempt]
        transport, close = transports[attempt]
        try:
            close.assert_not_called()
            assert process is not None and process.poll() is None
            assert transport.health_check()
            transport.write_file("/remote-ownership.txt", f"attempt {attempt}".encode())
            assert transport.read_file("/remote-ownership.txt") == f"attempt {attempt}".encode()
        finally:
            filesystem.close()
            filesystem.close()
        close.assert_called_once()
        assert process.poll() is not None
        assert kernel._transport is None
        assert ephemeral_dir is not None and not Path(ephemeral_dir).exists()
        assert remote_peer._transport.health_check()


@pytest.mark.parametrize("phase", ["vfs_stub", "search_stub", "error_handler"])
def test_transport_construction_failure_closes_its_channel(
    remote_peer: KernelClient, monkeypatch: pytest.MonkeyPatch, phase: str
):
    import grpc

    channel = grpc.insecure_channel(remote_peer._server_address)
    close = Mock(wraps=channel.close)
    monkeypatch.setattr(channel, "close", close)
    monkeypatch.setattr(grpc, "insecure_channel", lambda *args, **kwargs: channel)
    target = {
        "vfs_stub": "nexus.remote.rpc_transport.vfs_pb2_grpc.NexusVFSServiceStub",
        "search_stub": "nexus.remote.search_client.SearchClient",
        "error_handler": "nexus.remote.rpc_transport.BaseRemoteNexusFS",
    }[phase]
    failure = RuntimeError(f"Failed during {phase}")

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(target, fail)
    try:
        with pytest.raises(RuntimeError) as caught:
            RPCTransport(remote_peer._server_address)
        assert caught.value is failure
        close.assert_called_once()
        assert remote_peer._transport.health_check()
    finally:
        channel.close()
