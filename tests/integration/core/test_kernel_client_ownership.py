"""Filesystem close releases its subprocess and preserves borrowed clients."""

import shutil
from pathlib import Path

import pytest

import nexus
from nexus.cli.utils import connect_local_workspace
from nexus.core.nexus_fs import NexusFS
from nexus.remote.kernel_client import KernelClient, _resolve_kernel_binary


@pytest.fixture(autouse=True)
def require_cluster_binary():
    if shutil.which(_resolve_kernel_binary()) is None:
        pytest.skip("Kernel ownership tests require nexusd-cluster on PATH")


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_close_reaps_owned_kernel_and_releases_metastore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_fails: bool
):
    path = tmp_path / "namespace.redb"
    filesystem = NexusFS(metadata_store=path)
    kernel = filesystem._kernel
    assert kernel is not None
    process = kernel._process
    assert process is not None and process.poll() is None
    if cleanup_fails:

        def fail_service_close():
            raise RuntimeError("Service cleanup failed")

        monkeypatch.setattr(kernel, "service_close_all", fail_service_close)

    filesystem.close()
    filesystem.close()
    assert process.poll() is not None

    reopened = NexusFS(metadata_store=path)
    try:
        assert reopened._kernel is not None
        assert reopened._kernel._transport.health_check()
    finally:
        reopened.close()


def test_close_preserves_borrowed_kernel(tmp_path: Path):
    kernel = KernelClient(metadata_path=str(tmp_path / "namespace.redb"))
    kernel.open()
    process = kernel._process
    try:
        filesystem = NexusFS(metadata_store=kernel)
        filesystem.close()
        assert process is not None and process.poll() is None
        assert kernel._transport is not None and kernel._transport.health_check()
    finally:
        kernel.close()
    assert process.poll() is not None


def test_failed_connect_reaps_its_kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    processes = []

    def fail_restore(filesystem):
        processes.append(filesystem._kernel._process)
        raise RuntimeError("Mount restoration failed")

    monkeypatch.setattr(nexus, "_restore_mounts", fail_restore)
    with pytest.raises(RuntimeError, match="Mount restoration failed"):
        connect_local_workspace(str(tmp_path / "workspace"))
    assert len(processes) == 1
    assert processes[0].poll() is not None
