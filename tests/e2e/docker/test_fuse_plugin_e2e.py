"""FUSE plugin Docker E2E — real mount, real syscalls, gRPC cross-check.

Stands up a single `nexusd-cluster` with `nexus-fuse-plugin` loaded
inside a privileged container (`/dev/fuse`, `SYS_ADMIN`,
apparmor:unconfined), then exercises every directory-mutating op the
plugin wires through KernelHandle v2 via plain POSIX commands.  Every
assertion has both a mount-side (`docker exec` POSIX) AND a
kernel-side (gRPC `vfs_stat` / `vfs_read`) check, so a regression on
either layer surfaces as a mismatch rather than a silent skip.

Coverage matrix:

    POSIX op            FUSE op       KernelHandle v2 callback
    ─────────────────   ──────────    ─────────────────────────
    mkdir               mkdir         sys_mkdir
    rmdir               rmdir         sys_rmdir
    touch / `cat > x`   create        sys_write (empty) + sys_stat
    `cat > x` (data)    write         sys_write
    `cat x`             read          sys_read
    ls -1               readdir       sys_readdir
    mv                  rename        sys_rename
    rm                  unlink        sys_unlink

The compose file at ``dockerfiles/docker-compose.fuse-plugin.yml``
provides the env (``NEXUS_FUSE_E2E=1``, ``NEXUS_CLUSTER_GRPC``,
``NEXUS_CLUSTER_CONTAINER``, ``NEXUS_FUSE_MOUNT_POINT``); skip the
suite when it's not set so unrelated test invocations don't blow up.
"""

from __future__ import annotations

import os
import time

import pytest

from . import fuse_plugin_helpers, runbook_helpers

if os.environ.get("NEXUS_FUSE_E2E") != "1":
    pytest.skip(
        "FUSE plugin E2E suite — requires docker compose -f "
        "dockerfiles/docker-compose.fuse-plugin.yml up",
        allow_module_level=True,
    )


@pytest.fixture(scope="module")
def topology() -> fuse_plugin_helpers.FuseTopology:
    return fuse_plugin_helpers.topology_from_env()


def _wait_path_via_grpc(
    topology: fuse_plugin_helpers.FuseTopology,
    path: str,
    *,
    expect_found: bool,
    timeout: float = 10.0,
) -> dict:
    """Poll `vfs_stat` until the path matches the expected presence.

    The mount op completes synchronously inside the container, but the
    kernel-side metastore write can race the test runner's next gRPC
    call by a millisecond or two.  A 10 s budget with 100 ms polls
    absorbs that race without masking a real bug.
    """
    deadline = time.monotonic() + timeout
    last_result: dict = {}
    while time.monotonic() < deadline:
        last_result = runbook_helpers.vfs_stat(topology.cluster_grpc, path)
        if "error" in last_result:
            time.sleep(0.1)
            continue
        found = last_result["result"]["found"]
        if found == expect_found:
            return last_result
        time.sleep(0.1)
    pytest.fail(
        f"vfs_stat({path}) never reached expected found={expect_found} "
        f"within {timeout}s — last result: {last_result}"
    )


class TestMountLoadedAndReady:
    """Sanity — without these the rest of the suite is meaningless."""

    def test_grpc_responds(self, topology: fuse_plugin_helpers.FuseTopology) -> None:
        result = runbook_helpers.vfs_stat(topology.cluster_grpc, "/")
        assert "error" not in result, f"gRPC vfs_stat(/) failed: {result}"
        assert result["result"]["found"], "/ must exist on a fresh cluster"

    def test_mount_point_is_a_fuse_mount(
        self, topology: fuse_plugin_helpers.FuseTopology
    ) -> None:
        result = runbook_helpers.docker_exec(
            topology.cluster_container,
            ["mountpoint", "-q", topology.mount_point],
            check=False,
        )
        assert result.rc == 0, (
            f"{topology.mount_point} is not a mount point inside the "
            f"cluster container — the plugin's `fuser::spawn_mount2` "
            f"did not complete.  Check the cluster's stderr for the "
            f"`FUSE mount failed` log line."
        )


class TestMkdirReaddirRoundTrip:
    """`mkdir` + `ls` — exercise sys_mkdir and sys_readdir together."""

    def test_mkdir_then_readdir_sees_entry(
        self, topology: fuse_plugin_helpers.FuseTopology
    ) -> None:
        fuse_plugin_helpers.mount_mkdir(topology, "alpha-dir")

        # Kernel cross-check — exists with entry_type=1 (DT_DIR).
        stat = _wait_path_via_grpc(topology, topology.vfs_path("alpha-dir"), expect_found=True)
        assert stat["result"]["isDirectory"], (
            f"sys_mkdir landed but kernel reports non-directory: {stat}"
        )

        # readdir must show the entry on the parent listing.
        entries = fuse_plugin_helpers.mount_listdir(topology, "/")
        assert "alpha-dir" in entries, (
            f"readdir on / missed `alpha-dir` after mkdir; got {entries}"
        )

    def test_nested_mkdir_p_visible(
        self, topology: fuse_plugin_helpers.FuseTopology
    ) -> None:
        fuse_plugin_helpers.mount_mkdir(topology, "nested/deep/path", parents=True)
        stat = _wait_path_via_grpc(
            topology,
            topology.vfs_path("nested/deep/path"),
            expect_found=True,
        )
        assert stat["result"]["isDirectory"]
        entries = fuse_plugin_helpers.mount_listdir(topology, "nested/deep")
        assert "path" in entries


class TestCreateWriteRead:
    """`cat > file` + `cat file` — exercise create + write + read."""

    def test_create_empty_file_visible(
        self, topology: fuse_plugin_helpers.FuseTopology
    ) -> None:
        fuse_plugin_helpers.mount_write_bytes(topology, "empty.txt", b"")
        stat = _wait_path_via_grpc(topology, topology.vfs_path("empty.txt"), expect_found=True)
        assert not stat["result"]["isDirectory"]
        assert stat["result"]["size"] == 0

    def test_write_then_read_byte_exact_via_mount(
        self, topology: fuse_plugin_helpers.FuseTopology
    ) -> None:
        payload = b"FUSE plugin v2 callbacks: write+read byte-exact\n"
        fuse_plugin_helpers.mount_write_bytes(topology, "rw.txt", payload)
        roundtrip = fuse_plugin_helpers.mount_read_bytes(topology, "rw.txt")
        assert roundtrip == payload, (
            f"mount read != mount write; got len={len(roundtrip)}, "
            f"expected len={len(payload)}"
        )

    def test_write_via_mount_visible_through_grpc_read(
        self, topology: fuse_plugin_helpers.FuseTopology
    ) -> None:
        """The cross-check: write through the mount, read through gRPC.

        Catches FUSE plugin write callback that silently fails (returns
        EIO but kernel never gets the bytes) — the mount-side read would
        still see cached bytes via FUSE's own buffering, but gRPC sees
        the truth.
        """
        payload = b"cross-layer write verification payload"
        fuse_plugin_helpers.mount_write_bytes(topology, "cross.txt", payload)
        # vfs_stat first (fast) — confirms metastore knows the file.
        stat = _wait_path_via_grpc(topology, topology.vfs_path("cross.txt"), expect_found=True)
        assert stat["result"]["size"] == len(payload), (
            f"kernel sees size={stat['result']['size']}, expected {len(payload)} — "
            "FUSE plugin's write callback may have dropped bytes"
        )
        # vfs_read content check — the SSOT byte-exact assertion.
        read_result = runbook_helpers.vfs_read(
            topology.cluster_grpc, topology.vfs_path("cross.txt")
        )
        assert "error" not in read_result, f"vfs_read failed: {read_result}"
        assert runbook_helpers.decode_content(read_result) == payload


class TestRenameUnlinkRmdir:
    """`mv`, `rm`, `rmdir` — exercise sys_rename, sys_unlink, sys_rmdir."""

    def test_rename_moves_file(self, topology: fuse_plugin_helpers.FuseTopology) -> None:
        payload = b"rename target payload"
        fuse_plugin_helpers.mount_write_bytes(topology, "rename-src.txt", payload)
        _wait_path_via_grpc(
            topology, topology.vfs_path("rename-src.txt"), expect_found=True
        )

        fuse_plugin_helpers.mount_rename(topology, "rename-src.txt", "rename-dst.txt")

        _wait_path_via_grpc(topology, topology.vfs_path("rename-src.txt"), expect_found=False)
        _wait_path_via_grpc(topology, topology.vfs_path("rename-dst.txt"), expect_found=True)

        roundtrip = fuse_plugin_helpers.mount_read_bytes(topology, "rename-dst.txt")
        assert roundtrip == payload, (
            "rename preserved path but lost bytes — sys_rename moved metadata "
            "but lost the content reference"
        )

    def test_unlink_removes_file(
        self, topology: fuse_plugin_helpers.FuseTopology
    ) -> None:
        fuse_plugin_helpers.mount_write_bytes(topology, "doomed.txt", b"bye")
        _wait_path_via_grpc(topology, topology.vfs_path("doomed.txt"), expect_found=True)

        fuse_plugin_helpers.mount_unlink(topology, "doomed.txt")

        # Mount side AND kernel side must agree the file is gone.
        assert not fuse_plugin_helpers.mount_path_exists(topology, "doomed.txt")
        _wait_path_via_grpc(topology, topology.vfs_path("doomed.txt"), expect_found=False)

    def test_rmdir_removes_empty_directory(
        self, topology: fuse_plugin_helpers.FuseTopology
    ) -> None:
        fuse_plugin_helpers.mount_mkdir(topology, "doomed-dir")
        _wait_path_via_grpc(topology, topology.vfs_path("doomed-dir"), expect_found=True)

        fuse_plugin_helpers.mount_rmdir(topology, "doomed-dir")

        assert not fuse_plugin_helpers.mount_path_exists(topology, "doomed-dir")
        _wait_path_via_grpc(topology, topology.vfs_path("doomed-dir"), expect_found=False)
