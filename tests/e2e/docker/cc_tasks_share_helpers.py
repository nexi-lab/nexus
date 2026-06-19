"""Shared helpers for the cc-tasks-share E2E suite.

Sits next to :mod:`tests.e2e.docker.runbook_helpers` and reuses every
gRPC primitive from it.  This module groups three layers of helpers,
matching the three plugin layers each node carries:

* Topology — founder + joiner sharing a `sharedzone`.  Each node
  mounts its own LocalConnector backend at a hostname-namespaced
  path under ``/shared/cc-tasks/`` AND publishes that federated path
  back as a real OS mount via the FUSE plugin at ``/mnt/cc-tasks``.

* Host-fs (``/host/tasks/``) helpers — ``host_task_write`` /
  ``host_task_read`` / ``host_task_symlink``.  These read / write
  bytes at the LocalConnector's ``local_root``, i.e. the operator's
  ``~/.claude/tasks/`` analogue.  The LocalConnector's SSOT property
  says: after a ``vfs_write`` through gRPC, the bytes are physically
  present at ``/host/tasks/<rel>`` and vice versa.

* FUSE mount (``/mnt/cc-tasks``) helpers — ``mount_mkdir`` /
  ``mount_listdir`` / ``mount_write_bytes`` / etc.  These exercise
  the operator-facing OS-mount surface: a plain POSIX ``mkdir`` or
  ``cat`` inside the container flows through the kernel FUSE driver
  → ``nexus-fuse-plugin``'s background event loop → KernelHandle v3
  callbacks → the LocalConnector backend → the host fs.  The full
  Mac↔Win ``cc tasks list`` path, byte-for-byte.

Every helper is container-parameterised: pass
``topology.founder_container`` or ``topology.joiner_container`` to
target either node, which is the shape the cross-node workflow tests
need to flow data founder→joiner or joiner→founder.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

import pytest

from . import runbook_helpers


@dataclass(frozen=True)
class CcTasksTopology:
    """Founder + joiner topology backing the cc-tasks-share suite."""

    founder_grpc: str
    founder_container: str
    joiner_grpc: str
    joiner_container: str
    local_connector_zone: str
    local_connector_vfs_root: str
    fuse_mount_point: str
    fuse_vfs_root: str

    def vfs_path(self, relpath: str) -> str:
        """Compose the gRPC path under the *founder's* mount root.

        Used by the single-node substrate tests where every write
        happens on founder.  Cross-node tests compose their own
        per-node paths with explicit hostname namespacing so the
        data flow between nodes is unambiguous.
        """
        if not relpath.startswith("/"):
            relpath = "/" + relpath
        return f"{self.local_connector_vfs_root.rstrip('/')}{relpath}"

    def founder_vfs_path(self, relpath: str) -> str:
        """`/shared/cc-tasks/founder/<rel>` — founder's projection."""
        if not relpath.startswith("/"):
            relpath = "/" + relpath
        return f"/shared/cc-tasks/founder{relpath}"

    def joiner_vfs_path(self, relpath: str) -> str:
        """`/shared/cc-tasks/joiner/<rel>` — joiner's projection."""
        if not relpath.startswith("/"):
            relpath = "/" + relpath
        return f"/shared/cc-tasks/joiner{relpath}"

    def mount_path_for_vfs(self, vfs_path: str) -> str:
        """Convert a federated VFS path to the FUSE mount-side path.

        The FUSE plugin publishes ``fuse_vfs_root`` as ``fuse_mount_point``,
        so ``/shared/cc-tasks/founder/abc/1.json`` shows up at
        ``/mnt/cc-tasks/founder/abc/1.json`` on either node.  Used by
        the cross-layer workflows to assert the same bytes are visible
        through every surface.
        """
        root = self.fuse_vfs_root.rstrip("/")
        if not vfs_path.startswith(root + "/") and vfs_path != root:
            pytest.fail(
                f"mount_path_for_vfs: {vfs_path!r} is not under fuse_vfs_root={root!r}; "
                "either the test composed the wrong path or fuse_vfs_root drifted from "
                "the compose's NEXUS_FUSE_VFS_ROOT."
            )
        relative = vfs_path[len(root) :] or "/"
        return f"{self.fuse_mount_point.rstrip('/')}{relative}"


def topology_from_env() -> CcTasksTopology:
    return CcTasksTopology(
        founder_grpc=os.environ.get("NEXUS_FOUNDER_GRPC", "founder:2126"),
        founder_container=os.environ.get("NEXUS_FOUNDER_CONTAINER", "nexus-cc-tasks-founder"),
        joiner_grpc=os.environ.get("NEXUS_JOINER_GRPC", "joiner:2126"),
        joiner_container=os.environ.get("NEXUS_JOINER_CONTAINER", "nexus-cc-tasks-joiner"),
        local_connector_zone=os.environ.get("NEXUS_LOCAL_CONNECTOR_ZONE", "sharedzone"),
        local_connector_vfs_root=os.environ.get(
            "NEXUS_LOCAL_CONNECTOR_VFS_ROOT", "/shared/cc-tasks/founder"
        ),
        fuse_mount_point=os.environ.get("NEXUS_FUSE_MOUNT_POINT", "/mnt/cc-tasks"),
        fuse_vfs_root=os.environ.get("NEXUS_FUSE_VFS_ROOT", "/shared/cc-tasks"),
    )


# ---------------------------------------------------------------------------
# Host-fs (`/host/tasks/`) write / read / symlink helpers
#
# These talk to the same physical bytes the LocalConnector backend
# touches — the round-trip through `vfs_write` → host fs → `vfs_read`
# is what makes the dylib's SSOT property observable.
# ---------------------------------------------------------------------------
def _host_path(relpath: str) -> str:
    if not relpath.startswith("/"):
        relpath = "/" + relpath
    return f"/host/tasks{relpath}"


def host_task_write(container: str, relpath: str, data: bytes) -> None:
    """Write ``data`` to ``<container>:/host/tasks/<relpath>`` via docker exec.

    Bytes-preserving — the helper does NOT pass `text=True`, so binary
    content round-trips exactly.  Parents are created as needed; the
    operator-side analogue (Claude Code creating
    `tasks/<session>/<n>.json`) does this at the OS level too.
    """
    full_path = _host_path(relpath)
    parent = os.path.dirname(full_path) or "/"
    runbook_helpers.docker_exec(container, ["mkdir", "-p", parent], check=True)
    proc = subprocess.run(
        ["docker", "exec", "-i", container, "sh", "-c", f"cat > {full_path}"],
        input=data,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"host_task_write({container}, {relpath}) failed (rc={proc.returncode}): "
            f"stderr={proc.stderr.decode(errors='replace')}"
        )


def host_task_read(container: str, relpath: str) -> bytes:
    """Read ``<container>:/host/tasks/<relpath>`` via docker exec — bytes-preserving."""
    full_path = _host_path(relpath)
    proc = subprocess.run(
        ["docker", "exec", container, "cat", full_path],
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"host_task_read({container}, {relpath}) failed (rc={proc.returncode}): "
            f"stderr={proc.stderr.decode(errors='replace')}"
        )
    return proc.stdout


def host_task_ensure_dir(container: str, relpath: str) -> None:
    """``mkdir -p`` under ``<container>:/host/tasks/<relpath>``."""
    runbook_helpers.docker_exec(container, ["mkdir", "-p", _host_path(relpath)], check=True)


def host_task_symlink(container: str, link_relpath: str, target_relpath: str) -> None:
    """Create ``link_relpath`` -> ``target_relpath`` inside ``/host/tasks``.

    ``target_relpath`` may be absolute (e.g. ``/etc/passwd``) or
    relative.  Used by the symlink-escape security test to plant a
    link the backend's ``resolve_path`` is supposed to reject.
    """
    link_full = _host_path(link_relpath)
    parent = os.path.dirname(link_full) or "/"
    runbook_helpers.docker_exec(container, ["mkdir", "-p", parent], check=True)
    runbook_helpers.docker_exec(container, ["ln", "-sf", target_relpath, link_full], check=True)


# ---------------------------------------------------------------------------
# FUSE mount (`/mnt/cc-tasks`) POSIX ops, anchored at the mount point.
#
# Every helper here calls `docker exec` against the chosen container so
# the op flows through the in-container kernel + FUSE driver — the path
# the nexus-fuse-plugin actually wires.  Running ops on the host would
# either bypass the mount or exercise a different libfuse3 build
# entirely, neither of which exercises the plugin under test.
#
# Args: `container` = nexus-cc-tasks-founder or nexus-cc-tasks-joiner;
#       `mount_path` = absolute path under /mnt/cc-tasks (compose with
#                      `topology.mount_path_for_vfs(vfs_path)`).
# ---------------------------------------------------------------------------
def mount_mkdir(container: str, mount_path: str, *, parents: bool = False) -> None:
    """``mkdir [-p] <mount_path>`` — triggers the FUSE ``mkdir`` op."""
    cmd = ["mkdir"]
    if parents:
        cmd.append("-p")
    cmd.append(mount_path)
    runbook_helpers.docker_exec(container, cmd, check=True)


def mount_rmdir(container: str, mount_path: str) -> None:
    """``rmdir <mount_path>`` — triggers the FUSE ``rmdir`` op."""
    runbook_helpers.docker_exec(container, ["rmdir", mount_path], check=True)


def mount_unlink(container: str, mount_path: str) -> None:
    """``rm <mount_path>`` — triggers the FUSE ``unlink`` op."""
    runbook_helpers.docker_exec(container, ["rm", mount_path], check=True)


def mount_rename(container: str, old_mount_path: str, new_mount_path: str) -> None:
    """``mv <old> <new>`` — triggers the FUSE ``rename`` op."""
    runbook_helpers.docker_exec(container, ["mv", old_mount_path, new_mount_path], check=True)


def mount_listdir(container: str, mount_path: str) -> list[str]:
    """``ls -1 <mount_path>`` — triggers the FUSE ``readdir`` op.

    Returns the entry names sorted for stable assertions.  An empty
    directory returns ``[]`` (not a one-element list with empty string).
    """
    result = runbook_helpers.docker_exec(container, ["ls", "-1", mount_path], check=True)
    return sorted(line for line in result.stdout.splitlines() if line)


def mount_write_bytes(container: str, mount_path: str, data: bytes) -> None:
    """Write ``data`` to ``<container>:<mount_path>`` via FUSE.

    Bytes-preserving — runs through ``docker exec -i ... sh -c 'cat >
    <path>'`` so binary content round-trips exactly.  The ``cat``
    triggers ``create`` (file didn't exist) + ``write`` (the payload),
    both routing through the plugin's KernelHandle v3 callbacks.
    """
    proc = subprocess.run(
        ["docker", "exec", "-i", container, "sh", "-c", f"cat > {mount_path}"],
        input=data,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"mount_write_bytes({container}, {mount_path}) failed (rc={proc.returncode}): "
            f"stderr={proc.stderr.decode(errors='replace')}"
        )


def mount_read_bytes(container: str, mount_path: str) -> bytes:
    """Read ``<container>:<mount_path>`` byte-exact via ``docker exec ... cat``."""
    proc = subprocess.run(
        ["docker", "exec", container, "cat", mount_path],
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"mount_read_bytes({container}, {mount_path}) failed (rc={proc.returncode}): "
            f"stderr={proc.stderr.decode(errors='replace')}"
        )
    return proc.stdout


def mount_path_exists(container: str, mount_path: str) -> bool:
    """``test -e <mount_path>`` — surface the kernel's view of existence.

    Returns ``True`` when the in-container kernel-side FUSE driver
    reports the path exists at the moment of the call.  Used in
    negative assertions (after ``unlink`` / ``rmdir``) where the test
    must observe the absence, not just the lack of a positive signal.
    """
    proc = subprocess.run(
        ["docker", "exec", container, "test", "-e", mount_path],
        capture_output=True,
        timeout=10,
    )
    return proc.returncode == 0


def mount_is_fuse_mountpoint(container: str, mount_path: str) -> bool:
    """True if ``<mount_path>`` is a live FUSE mount inside ``<container>``.

    Uses ``/proc/mounts`` (the kernel SSOT) rather than ``mountpoint(1)``.
    ``mountpoint -q`` falsely returns rc=1 on FUSE mounts that compare
    st_dev with their parent dir under certain layered-FS configurations
    (Docker overlay2 + libfuse3 + Linux 5.x has been observed locally on
    Windows + GHA ubuntu-latest).  ``/proc/mounts`` always reflects
    reality the moment ``fuser::spawn_mount2`` returns.
    """
    proc = subprocess.run(
        [
            "docker",
            "exec",
            container,
            "sh",
            "-c",
            f"grep -qE ' {mount_path} fuse' /proc/mounts",
        ],
        capture_output=True,
        timeout=10,
    )
    return proc.returncode == 0
