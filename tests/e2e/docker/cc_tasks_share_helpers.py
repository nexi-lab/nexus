"""Shared helpers for the cc-tasks-share E2E suite.

Sits next to :mod:`tests.e2e.docker.runbook_helpers` and reuses every
gRPC primitive from it.  Only the helpers distinct to this suite live
here:

* Topology — founder + joiner sharing a `sharedzone`.  Each node
  mounts its own LocalConnector backend at a hostname-namespaced
  path under `/shared/cc-tasks/`.
* `host_task_write` / `host_task_read` / `host_task_symlink` — read
  / write / plant a symlink inside either container's `/host/tasks`
  named volume via ``docker exec``.  These verify the LocalConnector's
  "host fs is the SSOT" property: after a `vfs_write` through gRPC,
  the bytes are physically present at `/host/tasks/<rel>` and vice
  versa.

The host-fs helpers are container-parameterised: pass
`topology.founder_container` or `topology.joiner_container` to
target either node, which is the shape the cross-node workflow tests
(`TestCrossNodeLazyMaterialization`) need.
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

    def vfs_path(self, relpath: str) -> str:
        """Compose the gRPC path under the *founder's* mount root.

        Used by the single-node substrate tests
        (`TestLocalConnectorThroughVFS`) where every write happens on
        founder.  Cross-node tests compose their own per-node paths
        with explicit hostname namespacing so the data flow between
        nodes is unambiguous.
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
