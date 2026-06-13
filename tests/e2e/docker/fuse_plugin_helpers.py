"""Shared helpers for the FUSE plugin E2E suite.

Sits next to :mod:`tests.e2e.docker.runbook_helpers` and reuses every
gRPC primitive from it.  Only the helpers distinct to this suite live
here:

* Topology — single ``cluster`` node with the FUSE plugin loaded.  The
  mount point inside the container is whatever the compose's
  ``NEXUS_FUSE_MOUNT_POINT`` is set to (``/mnt/nexus`` by default).
* ``mount_exec`` — run a POSIX op (``mkdir`` / ``touch`` / ``ls`` /
  ``cat`` / ``mv`` / ``rm``) inside the cluster container, anchored
  at the FUSE mount point.  Every op flows through the kernel FUSE
  driver → the plugin's background thread → the v2 KernelHandle
  callbacks → the kernel syscall surface.
* ``mount_write_bytes`` / ``mount_read_bytes`` — byte-exact I/O to a
  file inside the mount via ``docker exec`` with stdin/stdout
  pipelines.  Used to cross-check ``vfs_read`` returns the same bytes
  ``cat`` saw at the mount.

The cross-check pattern is symmetric: every assertion has a POSIX-op
side (through the mount) AND a gRPC ``vfs_stat`` / ``vfs_read`` side
(direct to the kernel).  A regression in either layer surfaces as a
mismatch, not a silent skip.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

import pytest

from . import runbook_helpers


@dataclass(frozen=True)
class FuseTopology:
    """Single-node cluster + FUSE-plugin topology backing the suite."""

    cluster_grpc: str
    cluster_container: str
    mount_point: str

    def vfs_path(self, relpath: str) -> str:
        """Compose the kernel-side VFS path for a relpath under the mount.

        ``NEXUS_FUSE_VFS_ROOT`` is ``/`` in the compose, so the mount
        projects the kernel's VFS root onto the container path.  A
        relpath of ``foo/bar.txt`` thus translates to kernel-side
        ``/foo/bar.txt`` — the same path ``vfs_stat`` / ``vfs_read``
        consume.
        """
        if not relpath.startswith("/"):
            relpath = "/" + relpath
        return relpath

    def mount_path(self, relpath: str) -> str:
        """Compose the container-side path under the FUSE mount.

        Used as the second argument to every ``docker exec`` POSIX op
        — the path the kernel's FUSE driver sees.
        """
        if not relpath.startswith("/"):
            relpath = "/" + relpath
        return f"{self.mount_point.rstrip('/')}{relpath}"


def topology_from_env() -> FuseTopology:
    return FuseTopology(
        cluster_grpc=os.environ.get("NEXUS_CLUSTER_GRPC", "cluster:2126"),
        cluster_container=os.environ.get("NEXUS_CLUSTER_CONTAINER", "nexus-fuse-cluster"),
        mount_point=os.environ.get("NEXUS_FUSE_MOUNT_POINT", "/mnt/nexus"),
    )


# ---------------------------------------------------------------------------
# POSIX ops inside the cluster container, anchored at the FUSE mount.
#
# Every helper here ultimately calls ``docker exec`` against the cluster.
# Using ``docker exec`` (not the local host) ensures the ops go through
# the in-container kernel + FUSE driver — the path the plugin actually
# wires.  Running ops on the host would either bypass the mount or
# exercise a different libfuse3 build entirely.
# ---------------------------------------------------------------------------
def mount_mkdir(topology: FuseTopology, relpath: str, *, parents: bool = False) -> None:
    """``mkdir [-p] <mount>/<relpath>`` — triggers the plugin's ``mkdir`` op."""
    cmd = ["mkdir"]
    if parents:
        cmd.append("-p")
    cmd.append(topology.mount_path(relpath))
    runbook_helpers.docker_exec(topology.cluster_container, cmd, check=True)


def mount_rmdir(topology: FuseTopology, relpath: str) -> None:
    """``rmdir <mount>/<relpath>`` — triggers the plugin's ``rmdir`` op."""
    runbook_helpers.docker_exec(
        topology.cluster_container,
        ["rmdir", topology.mount_path(relpath)],
        check=True,
    )


def mount_unlink(topology: FuseTopology, relpath: str) -> None:
    """``rm <mount>/<relpath>`` — triggers the plugin's ``unlink`` op."""
    runbook_helpers.docker_exec(
        topology.cluster_container,
        ["rm", topology.mount_path(relpath)],
        check=True,
    )


def mount_rename(topology: FuseTopology, old_relpath: str, new_relpath: str) -> None:
    """``mv <mount>/<old> <mount>/<new>`` — triggers the plugin's ``rename`` op."""
    runbook_helpers.docker_exec(
        topology.cluster_container,
        ["mv", topology.mount_path(old_relpath), topology.mount_path(new_relpath)],
        check=True,
    )


def mount_listdir(topology: FuseTopology, relpath: str = "/") -> list[str]:
    """``ls -1 <mount>/<relpath>`` — triggers the plugin's ``readdir`` op.

    Returns the entry names sorted for stable assertions.  An empty
    directory returns ``[]`` (not a one-element list with empty string).
    """
    result = runbook_helpers.docker_exec(
        topology.cluster_container,
        ["ls", "-1", topology.mount_path(relpath)],
        check=True,
    )
    names = [line for line in result.stdout.splitlines() if line]
    return sorted(names)


def mount_write_bytes(topology: FuseTopology, relpath: str, data: bytes) -> None:
    """Write ``data`` to ``<mount>/<relpath>``.

    Bytes-preserving — runs through ``docker exec -i ... sh -c 'cat >
    <path>'`` so binary content round-trips exactly.  The ``cat``
    triggers ``create`` (file didn't exist) + ``write`` (the payload),
    both routing through the plugin's KernelHandle callbacks.
    """
    full_path = topology.mount_path(relpath)
    proc = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            topology.cluster_container,
            "sh",
            "-c",
            f"cat > {full_path}",
        ],
        input=data,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"mount_write_bytes({relpath}) failed (rc={proc.returncode}): "
            f"stderr={proc.stderr.decode(errors='replace')}"
        )


def mount_read_bytes(topology: FuseTopology, relpath: str) -> bytes:
    """Read ``<mount>/<relpath>`` byte-exact via ``docker exec ... cat``."""
    proc = subprocess.run(
        ["docker", "exec", topology.cluster_container, "cat", topology.mount_path(relpath)],
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"mount_read_bytes({relpath}) failed (rc={proc.returncode}): "
            f"stderr={proc.stderr.decode(errors='replace')}"
        )
    return proc.stdout


def mount_path_exists(topology: FuseTopology, relpath: str) -> bool:
    """``test -e <mount>/<relpath>`` — surface the kernel's view of existence.

    Returns ``True`` when the kernel-side FUSE driver reports the path
    exists at the moment of the call.  Used in negative assertions
    (after ``unlink`` / ``rmdir``) where the test must observe the
    absence, not just the lack of a positive signal.
    """
    proc = subprocess.run(
        [
            "docker",
            "exec",
            topology.cluster_container,
            "test",
            "-e",
            topology.mount_path(relpath),
        ],
        capture_output=True,
        timeout=10,
    )
    return proc.returncode == 0
