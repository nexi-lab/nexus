"""Shared helpers for the cc-tasks-share E2E suite.

Sits next to :mod:`tests.e2e.docker.runbook_helpers` and reuses every
gRPC + raft catch-up gate from it.  Only what's distinct lives here:

* Topology resolution for the 2-voter sharedzone (no witness — see
  the compose file header for why).
* `host_task_write` / `host_task_read` — write / read files inside a
  cluster container's `/host/tasks` named volume via ``docker exec``.
  These simulate the operator-side `~/.claude/tasks/<session>/<n>.json`
  surface that the LocalConnector dylib projects into the VFS.

Using ``docker exec`` (rather than bind-mounting a host directory)
keeps the suite hermetic across CI runners: the compose file's
``cc_tasks_*_hostfs`` named volumes live entirely inside the docker
daemon, and the test runner reaches them through the same
``docker exec`` it already uses for the offline-join sidecar pattern
from the runbook suite.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

import pytest

from . import runbook_helpers


@dataclass(frozen=True)
class CcTasksTopology:
    """Mirror of :class:`runbook_helpers.RunbookTopology` without the witness slot."""

    founder_grpc: str
    joiner_grpc: str
    founder_container: str
    joiner_container: str

    @property
    def all_voters_grpc(self) -> list[str]:
        return [self.founder_grpc, self.joiner_grpc]


def topology_from_env() -> CcTasksTopology:
    return CcTasksTopology(
        founder_grpc=os.environ.get("NEXUS_FOUNDER_GRPC", "founder:2126"),
        joiner_grpc=os.environ.get("NEXUS_JOINER_GRPC", "joiner:2126"),
        founder_container=os.environ.get("NEXUS_FOUNDER_CONTAINER", "nexus-cc-tasks-founder"),
        joiner_container=os.environ.get("NEXUS_JOINER_CONTAINER", "nexus-cc-tasks-joiner"),
    )


# ---------------------------------------------------------------------------
# Host-fs (`/host/tasks/`) write / read helpers
#
# `mkdir -p` is part of the write so callers don't have to pre-stage
# parent directories — the LocalConnector backend creates parents on
# write through the VFS surface, but the operator analogue (Claude
# Code creating `tasks/<session>/<n>.json`) does it at the OS level
# first.
# ---------------------------------------------------------------------------
def _host_path(relpath: str) -> str:
    if not relpath.startswith("/"):
        relpath = "/" + relpath
    return f"/host/tasks{relpath}"


def host_task_write(container: str, relpath: str, data: bytes) -> None:
    """Write ``data`` to ``<container>:/host/tasks/<relpath>`` via docker exec.

    ``relpath`` is interpreted relative to the LocalConnector's
    ``local_root`` — the test asserts the same bytes show up at
    ``/shared/cc-tasks/<hostname>/<relpath>`` through the VFS.
    """
    full_path = _host_path(relpath)
    parent = os.path.dirname(full_path) or "/"
    runbook_helpers.docker_exec(container, ["mkdir", "-p", parent], check=True)
    # `cat > <path>` round-trips raw bytes; subprocess input=data with
    # bytes (no text mode) preserves binary content exactly.
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

    Both paths are relative to the LocalConnector's ``local_root``.
    Used by symlink-escape security tests to plant the link the
    backend's `resolve_path` is supposed to reject.
    """
    link_full = _host_path(link_relpath)
    parent = os.path.dirname(link_full) or "/"
    runbook_helpers.docker_exec(container, ["mkdir", "-p", parent], check=True)
    runbook_helpers.docker_exec(container, ["ln", "-sf", target_relpath, link_full], check=True)
