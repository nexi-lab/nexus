"""cc-tasks-share Docker E2E — LocalConnector dylib + federation.

Locks down the operator workflow underlying the Mac↔Win
``cc tasks list`` share milestone:

  1. Each daemon loads ``libnexus_local_connector.so`` from
     ``--plugin-dir`` and mounts ``/shared/cc-tasks/<hostname>``
     via ``--mount-driver`` against its own ``/host/tasks`` volume.
  2. A file written to one node's ``/host/tasks/<relpath>`` is
     readable on the peer at ``/shared/cc-tasks/<hostname>/<relpath>``
     through the federated sharedzone.

The four test methods correspond 1-to-1 to the plan's "Decisions
captured" section:

  * Founder→joiner byte-exact read (the primary milestone).
  * Joiner→founder byte-exact read (symmetric — proves both sides
    expose their LocalConnector through federation, not just the
    bootstrap node).
  * Symlink-escape rejection survives federation (the LocalConnector
    security property holds when the request comes in via the
    sharedzone leader instead of a direct local call).
  * Concurrent writers → eventual-consistency listdir (10 task JSONs
    arrive at the peer with no permanent missed entries).

Reuses :mod:`tests.e2e.docker.runbook_helpers` verbatim for gRPC,
raft catch-up, and offline-join lifecycle.
"""

from __future__ import annotations

import concurrent.futures
import os
import time

import pytest

from tests.e2e.docker import cc_tasks_share_helpers, runbook_helpers
from tests.e2e.docker.cc_tasks_share_helpers import CcTasksTopology


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
def joined_cluster(topology: CcTasksTopology, api_key: str) -> dict:
    """Drive the runbook §3b joiner CLI flow against the 2-voter sharedzone.

    Lifecycle mirrors `tests/e2e/docker/test_federation_runbook.py`'s
    `joined_cluster` fixture exactly — only topology object type and
    container/volume env-var names differ.
    """
    founder_node_id = runbook_helpers.fetch_node_id(topology.founder_container)
    joiner_volume = os.environ["NEXUS_JOINER_VOLUME"]

    runbook_helpers.docker_stop(topology.joiner_container)

    join_result = runbook_helpers.run_nexusd_cluster_join(
        target_container=topology.joiner_container,
        target_volume=joiner_volume,
        founder_node_id=founder_node_id,
        founder_addr="founder:2126",
        zone_id="sharedzone",
        local_path="/shared",
        hostname="joiner",
        data_dir="/app/data",
        timeout=120,
    )
    assert join_result.returncode == 0, (
        f"`nexusd-cluster join` exited non-zero.  rc={join_result.returncode}\n"
        f"stdout={join_result.stdout}\nstderr={join_result.stderr}"
    )

    runbook_helpers.docker_start(topology.joiner_container)
    runbook_helpers.wait_healthy([topology.joiner_grpc])
    runbook_helpers.wait_zone_ready(topology.joiner_grpc, "sharedzone", api_key=api_key, timeout=60)

    return {"founder_node_id": founder_node_id}


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------
class TestCcTasksShare:
    """LocalConnector dylib + federation E2E surface."""

    def test_peer_sees_founder_local_connector_write_byte_exact(
        self,
        topology: CcTasksTopology,
        api_key: str,
        joined_cluster: dict,
    ) -> None:
        """Founder host-fs write → joiner reads same bytes through federation.

        The primary milestone underlying the Mac↔Win ``cc tasks list``
        share: a task JSON written to one node's ``~/.claude/tasks/``
        appears at the peer's view of ``/shared/cc-tasks/<owner>/``.
        """
        relpath = "/session-a/1.json"
        payload = b'{"task":"founder-write","tags":["mac"]}'

        cc_tasks_share_helpers.host_task_write(topology.founder_container, relpath, payload)

        vfs_path = f"/shared/cc-tasks/founder{relpath}"
        runbook_helpers.wait_nodes_caught_up(
            [topology.joiner_grpc],
            "sharedzone",
            api_key=api_key,
            timeout=30,
            probe_path=vfs_path,
        )

        result = runbook_helpers.vfs_read(topology.joiner_grpc, vfs_path, api_key=api_key)
        assert "error" not in result, f"vfs_read failed: {result}"
        assert result["result"]["content"] == payload, (
            f"byte-exact mismatch: expected {payload!r}, got {result['result']['content']!r}"
        )

    def test_peer_sees_joiner_write_back_byte_exact(
        self,
        topology: CcTasksTopology,
        api_key: str,
        joined_cluster: dict,
    ) -> None:
        """Symmetric: joiner host-fs write → founder reads through federation.

        Proves the LocalConnector dylib is symmetric — both sides
        expose their own host fs through the federated path, not just
        the bootstrap node.
        """
        relpath = "/session-b/2.json"
        payload = b'{"task":"joiner-write","tags":["win"]}'

        cc_tasks_share_helpers.host_task_write(topology.joiner_container, relpath, payload)

        vfs_path = f"/shared/cc-tasks/joiner{relpath}"
        runbook_helpers.wait_nodes_caught_up(
            [topology.founder_grpc],
            "sharedzone",
            api_key=api_key,
            timeout=30,
            probe_path=vfs_path,
        )

        result = runbook_helpers.vfs_read(topology.founder_grpc, vfs_path, api_key=api_key)
        assert "error" not in result, f"vfs_read failed: {result}"
        assert result["result"]["content"] == payload, (
            f"byte-exact mismatch: expected {payload!r}, got {result['result']['content']!r}"
        )

    def test_localconnector_rejects_symlink_escape_via_federation(
        self,
        topology: CcTasksTopology,
        api_key: str,
        joined_cluster: dict,
    ) -> None:
        """Symlink escape stays rejected even when the request comes via federation.

        Belt-and-suspenders: the Rust LocalConnector's `resolve_path`
        escape check (root canonicalization + starts-with guard) is
        already covered by the unit suite at
        `rust/backends/local-connector/src/lib.rs`'s
        `symlink_escape_is_rejected` test.  This case confirms the
        guard still fires when the request enters the connector
        through federation forwarding rather than a direct local call.
        """
        cc_tasks_share_helpers.host_task_write(
            topology.founder_container, "/.real-target.json", b'{"x":1}'
        )
        cc_tasks_share_helpers.host_task_symlink(
            topology.founder_container,
            "/session-c/escape.json",
            "/etc/passwd",
        )

        vfs_path = "/shared/cc-tasks/founder/session-c/escape.json"
        # The read SHOULD surface an error (the federation surface
        # propagates the LocalConnector's PermissionDenied through the
        # gRPC error envelope); the bytes from /etc/passwd MUST NOT
        # leak back to the joiner.
        result = runbook_helpers.vfs_read(topology.joiner_grpc, vfs_path, api_key=api_key)
        if "result" in result:
            content = result["result"].get("content", b"")
            assert b"root:" not in content, (
                "symlink escape leaked /etc/passwd contents through federation"
            )

    def test_two_concurrent_writers_then_list_via_peer(
        self,
        topology: CcTasksTopology,
        api_key: str,
        joined_cluster: dict,
    ) -> None:
        """Concurrent writes on founder → joiner sees them all eventually.

        Soak test for the operator-side burst write pattern (Claude
        Code rewriting tasks rapidly).  Asserts every written file
        eventually shows up at the peer's federated path; does NOT
        assert ordering since the host-fs writes are independent.
        """
        session = "/session-burst"
        cc_tasks_share_helpers.host_task_ensure_dir(topology.founder_container, session)

        n = 10
        payloads = {
            f"{session}/{i}.json": f'{{"i":{i},"src":"founder"}}'.encode() for i in range(n)
        }

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(
                    cc_tasks_share_helpers.host_task_write,
                    topology.founder_container,
                    relpath,
                    data,
                )
                for relpath, data in payloads.items()
            ]
            for f in futures:
                f.result(timeout=30)

        # Wait for every one of the N files to appear on the joiner's
        # federated view.  Poll vfs_stat per file rather than vfs_list,
        # because vfs_list isn't part of the runbook_helpers typed-RPC
        # surface and the per-path Stat is what wait_nodes_caught_up
        # already uses.
        deadline = time.time() + 60
        pending = set(payloads.keys())
        while pending and time.time() < deadline:
            still_pending = set()
            for relpath in pending:
                vfs_path = f"/shared/cc-tasks/founder{relpath}"
                stat = runbook_helpers.vfs_stat(
                    topology.joiner_grpc, vfs_path, api_key=api_key, zone_id="sharedzone"
                )
                if "error" in stat or not stat.get("result", {}).get("found"):
                    still_pending.add(relpath)
            pending = still_pending
            if pending:
                time.sleep(0.5)

        assert not pending, (
            f"joiner never saw {len(pending)} of {n} concurrent writes "
            f"within 60s: {sorted(pending)}"
        )
