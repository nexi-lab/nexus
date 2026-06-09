"""cc-tasks-share Docker E2E — LocalConnector dylib at the operator surface.

Locks down the dylib + driver-plugin runtime path on a single node:

  1. The dylib loads via ``--plugin-dir``.
  2. ``--mount-driver local-connector:<zone>:<path>:<config>`` mounts the
     dylib into the operator-named non-root zone after the federation
     init gate flips.
  3. ``vfs_write`` through gRPC routes to the LocalConnector backend;
     bytes land at the configured ``local_root`` on the host fs.
  4. ``vfs_read`` returns those bytes; ``docker exec cat`` confirms
     host-fs SSOT (the LocalConnector's defining property).
  5. Symlink escape stays rejected when the read enters through the
     VFS gRPC surface.
  6. Concurrent ``vfs_write`` calls become observable through
     ``vfs_stat``.

The "share" goal — projecting one node's ``/host/tasks/`` into a
peer's view of the federated namespace — needs cross-node operator-
mount substrate that is out of scope here.  The operator recipe and
the deferred-substrate boundary live in
`docs/architecture/federation-cross-machine-runbook.md` §3e.
"""

from __future__ import annotations

import concurrent.futures
import os
import time

import pytest

from tests.e2e.docker import cc_tasks_share_helpers, runbook_helpers
from tests.e2e.docker.cc_tasks_share_helpers import CcTasksTopology

# Skip when not invoked from the dedicated workflow that brings the
# cluster up — same gate the federation-runbook suite uses (the
# compose file's `test` service sets `NEXUS_CC_TASKS_E2E=1`).
pytestmark = [
    pytest.mark.xdist_group("cc-tasks-share"),
    pytest.mark.skipif(
        os.environ.get("NEXUS_CC_TASKS_E2E") != "1",
        reason="cc-tasks-share E2E suite needs the docker-compose.cc-tasks-share stack; "
        "set NEXUS_CC_TASKS_E2E=1 to enable (cc-tasks-share-e2e.yml sets this automatically).",
    ),
]


# ---------------------------------------------------------------------------
# Module-scoped fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def topology() -> CcTasksTopology:
    return cc_tasks_share_helpers.topology_from_env()


@pytest.fixture(scope="module")
def api_key() -> str:
    return os.environ.get("NEXUS_API_KEY", "sk-test-cc-tasks-key")


@pytest.fixture(scope="module")
def ready_node(topology: CcTasksTopology, api_key: str) -> None:
    """Gate on the daemon's `--mount-driver` loop having installed the DT_MOUNT."""
    runbook_helpers.wait_healthy([topology.founder_grpc])
    # `vfs_stat` enters through root zone (zone_id default) so the
    # routing exercises the DT_MOUNT at /tasks → my-tasks that the
    # mount loop installs.  `found=False` is fine — what we gate on
    # is the absence of a routing error.  An `"error"` response is
    # the wedged-path signal that the DT_MOUNT hasn't landed yet.
    deadline = time.time() + 60
    while time.time() < deadline:
        stat = runbook_helpers.vfs_stat(
            topology.founder_grpc,
            topology.local_connector_vfs_root,
            api_key=api_key,
            timeout=5,
        )
        if "error" not in stat:
            return
        time.sleep(0.5)
    pytest.fail(
        f"`my-tasks` DT_MOUNT never became reachable on {topology.founder_grpc} "
        f"within 60s — did `--mount-driver` finish at boot?"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestLocalConnectorThroughVFS:
    """LocalConnector dylib + driver-plugin runtime path at the gRPC surface."""

    def test_vfs_write_lands_on_host_fs(
        self,
        topology: CcTasksTopology,
        api_key: str,
        ready_node: None,
    ) -> None:
        """vfs_write → LocalConnector backend → physical bytes at ``local_root``.

        This is the SSOT property the LocalConnector exists to
        guarantee: writes through the VFS appear directly on the host
        fs at the operator-configured ``local_root``.  No copy, no
        shadow.
        """
        relpath = "/session-a/1.json"
        payload = b'{"task":"vfs-write-lands-on-host-fs"}'

        result = runbook_helpers.vfs_write(
            topology.founder_grpc, topology.vfs_path(relpath), payload, api_key=api_key
        )
        assert "error" not in result, f"vfs_write failed: {result}"

        host_bytes = cc_tasks_share_helpers.host_task_read(topology.founder_container, relpath)
        assert host_bytes == payload, (
            f"host-fs SSOT broken: vfs_write produced {payload!r} but host fs has {host_bytes!r}"
        )

    def test_host_fs_write_visible_via_vfs(
        self,
        topology: CcTasksTopology,
        api_key: str,
        ready_node: None,
    ) -> None:
        """Bytes written directly to ``local_root`` appear via vfs_read.

        Symmetric to :py:meth:`test_vfs_write_lands_on_host_fs` — the
        SSOT property cuts both ways.  This is the operator-side
        workflow (Claude Code writing JSON to ``~/.claude/tasks/``)
        becoming visible through ``vfs_read`` syscalls.
        """
        relpath = "/session-b/2.json"
        payload = b'{"task":"host-fs-write-visible-via-vfs"}'

        cc_tasks_share_helpers.host_task_write(topology.founder_container, relpath, payload)

        result = runbook_helpers.vfs_read(
            topology.founder_grpc, topology.vfs_path(relpath), api_key=api_key
        )
        assert "error" not in result, f"vfs_read failed: {result}"
        assert result["result"]["content"] == payload, (
            f"vfs_read returned {result['result']['content']!r}, expected {payload!r}"
        )

    def test_localconnector_rejects_symlink_escape(
        self,
        topology: CcTasksTopology,
        api_key: str,
        ready_node: None,
    ) -> None:
        """A symlink pointing outside ``local_root`` stays rejected on read.

        Mirrors the Rust unit test
        ``rust/backends/local-connector/src/lib.rs::symlink_escape_is_rejected``
        but exercises the path through the VFS gRPC surface, so the
        backend's ``resolve_path`` escape check runs after kernel
        dispatch + DT_MOUNT routing.
        """
        cc_tasks_share_helpers.host_task_write(
            topology.founder_container, "/.real-target.json", b'{"x":1}'
        )
        cc_tasks_share_helpers.host_task_symlink(
            topology.founder_container,
            "/session-c/escape.json",
            "/etc/passwd",
        )

        result = runbook_helpers.vfs_read(
            topology.founder_grpc,
            topology.vfs_path("/session-c/escape.json"),
            api_key=api_key,
        )
        # The backend returns PermissionDenied which surfaces as a
        # gRPC error envelope OR an empty/non-matching content; either
        # way the bytes from /etc/passwd MUST NOT leak through.
        if "result" in result:
            content = result["result"].get("content", b"")
            assert b"root:" not in content, (
                "symlink escape leaked /etc/passwd contents through the VFS surface"
            )

    def test_concurrent_vfs_writes_become_visible(
        self,
        topology: CcTasksTopology,
        api_key: str,
        ready_node: None,
    ) -> None:
        """N parallel vfs_writes through gRPC; each becomes visible via vfs_stat.

        Soak coverage for the operator burst-write pattern (Claude Code
        rewriting tasks rapidly).  Asserts every write is observable
        through the LocalConnector path; ordering is not constrained.
        """
        n = 10
        payloads = {f"/session-burst/{i}.json": f'{{"i":{i}}}'.encode() for i in range(n)}

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(
                    runbook_helpers.vfs_write,
                    topology.founder_grpc,
                    topology.vfs_path(relpath),
                    data,
                    api_key=api_key,
                )
                for relpath, data in payloads.items()
            ]
            for fut in futures:
                write_result = fut.result(timeout=30)
                assert "error" not in write_result, f"vfs_write failed: {write_result}"

        for relpath in payloads:
            # Stat enters through root zone so the DT_MOUNT at /tasks
            # routes through to the LocalConnector's my-tasks view.
            stat = runbook_helpers.vfs_stat(
                topology.founder_grpc,
                topology.vfs_path(relpath),
                api_key=api_key,
                timeout=5,
            )
            assert "error" not in stat, f"vfs_stat failed for {relpath}: {stat}"
            assert stat["result"]["found"], (
                f"vfs_stat after vfs_write did not surface {relpath}: {stat}"
            )
