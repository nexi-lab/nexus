"""cc-tasks peer-shared Docker E2E — same-VFS-path merged view.

Companion to ``test_cc_tasks_share_e2e.py``: SAME plugin stack (nexus-vfs
kernel + LocalConnector driver + FUSE service), but exercises the
**peer-shared** mount shape where BOTH nodes mount their LocalConnector
at the SAME VFS path (``/shared/cc-tasks``, no per-hostname suffix).
This is the production Win↔Mac topology that ``start-founder.sh`` and
``federation-cross-machine-runbook.md`` deploy.

The namespaced companion suite (``test_cc_tasks_share_e2e.py``) covers
the historical ``/shared/cc-tasks/<hostname>`` shape.  Both shapes
exercise DT_MOUNT idempotency + raft-replicated metastore, but only
this suite exercises the same-VFS-path rebind semantics that make
``cc tasks list`` a flat merged UUID list on both peers.

Test workflows are 3+ steps with hard causal data flow between them.
Each test uses uuid4-namespaced session dirs + try/finally cleanup so
concurrent runs don't collide.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid

import pytest

from tests.e2e.docker import cc_tasks_share_helpers, runbook_helpers
from tests.e2e.docker.cc_tasks_share_helpers import CcTasksTopology

pytestmark = [
    pytest.mark.xdist_group("cc-tasks-peer-shared"),
    pytest.mark.skipif(
        os.environ.get("NEXUS_CC_TASKS_PEER_SHARED_E2E") != "1",
        reason="cc-tasks-peer-shared E2E suite needs the "
        "docker-compose.cc-tasks-peer-shared stack; set "
        "NEXUS_CC_TASKS_PEER_SHARED_E2E=1 to enable "
        "(cc-tasks-peer-shared-e2e.yml sets this automatically).",
    ),
]


# ---------------------------------------------------------------------------
# Fixtures (module-scoped, mirror the namespaced suite)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def topology() -> CcTasksTopology:
    return cc_tasks_share_helpers.topology_from_env()


@pytest.fixture(scope="module")
def api_key() -> str:
    return os.environ.get("NEXUS_API_KEY", "sk-test-cc-tasks-key")


@pytest.fixture(scope="module")
def joined_cluster(topology: CcTasksTopology) -> dict:
    """Bring joiner into founder's sharedzone via `nexusd-cluster join`.

    Peer-shared parity with the namespaced suite: joiner's compose
    entrypoint does NOT set FEDERATION_ZONES on first boot, so
    `sharedzone` only lands after the sidecar's offline join writes
    ConfState + DT_MOUNT entries into the data dir.  The main daemon
    then auto-replays --mount-driver on its restart.  Post-#109 the
    sidecar accepts bare `host:port` (no `<id>@` prefix).
    """
    founder_node_id = runbook_helpers.fetch_node_id(topology.founder_container)
    return runbook_helpers.run_nexusd_cluster_join(
        target_container=topology.joiner_container,
        target_volume="nexus-cc-tasks-ps-joiner-data",
        founder_node_id=founder_node_id,
        founder_addr="founder:2126",
        cluster_image=os.environ.get("NEXUS_CLUSTER_IMAGE", "nexus-local-connector-plugin:latest"),
        network=os.environ.get("NEXUS_CC_TASKS_NETWORK", "nexus-cc-tasks-ps-net"),
        zone_id="sharedzone",
        mount_path="/shared",
        identity_volume="nexus-cc-tasks-ps-joiner-identity",
    ) | {"founder_node_id": founder_node_id}


@pytest.fixture(scope="module")
def ready_stack(topology: CcTasksTopology, api_key: str, joined_cluster: dict) -> None:
    """Both nodes: gRPC + FUSE mount + sharedzone `/shared/cc-tasks` mount.

    Peer-shared invariant: after join, BOTH nodes have the SAME
    `sharedzone:/shared/cc-tasks` mount in their metastore.  Gate on
    both sides answering vfs_stat on that shared path (not per-host
    namespaced) — that's the differentiator vs the namespaced suite.
    """
    runbook_helpers.wait_healthy([topology.founder_grpc, topology.joiner_grpc])
    _ = joined_cluster  # implicit dependency — join happens before mount asserts

    for label, grpc, container in (
        ("founder", topology.founder_grpc, topology.founder_container),
        ("joiner", topology.joiner_grpc, topology.joiner_container),
    ):
        deadline = time.time() + 60
        while time.time() < deadline:
            stat = runbook_helpers.vfs_stat(grpc, "/shared/cc-tasks", api_key=api_key, timeout=5)
            if (
                "error" not in stat
                and stat.get("result", {}).get("found")
                and cc_tasks_share_helpers.mount_is_fuse_mountpoint(
                    container, topology.fuse_mount_point
                )
            ):
                break
            time.sleep(0.5)
        else:
            pytest.fail(
                f"{label} never reached ready state (gRPC + FUSE + "
                f"/shared/cc-tasks mount) within 60s"
            )


def _new_session() -> str:
    return f"session-{uuid.uuid4().hex[:12]}"


def _poll_for_bytes(
    container: str,
    mount_path: str,
    expected: bytes,
    failure_prefix: str,
    *,
    timeout: float = 30.0,
) -> None:
    """Poll ``cat <mount_path>`` until it returns ``expected``.

    Unlike ``cc_tasks_share_helpers.mount_read_bytes`` this tolerates
    transient failures (path not yet observed → ENOENT, peer fetch
    in flight → empty read).  Only a byte-exact match satisfies the
    poll; anything else keeps waiting until the deadline elapses.
    """
    deadline = time.monotonic() + timeout
    last_got: bytes | None = None
    last_stderr = ""
    while time.monotonic() < deadline:
        proc = subprocess.run(
            ["docker", "exec", container, "cat", mount_path],
            capture_output=True,
            timeout=30,
        )
        if proc.returncode == 0 and proc.stdout == expected:
            return
        last_got = proc.stdout
        last_stderr = proc.stderr.decode(errors="replace")
        time.sleep(0.5)
    pytest.fail(
        f"{failure_prefix} within {timeout}s (expected {expected!r}, "
        f"last got {last_got!r}, last stderr={last_stderr!r})"
    )


# ---------------------------------------------------------------------------
# Workflow 1: merged view — each node writes to its own host fs, both
# sides see the union via metastore observation.
# ---------------------------------------------------------------------------
class TestPeerSharedMergedView:
    """Each side writes ONE session; both sides see BOTH.

    Peer-shared invariant: with BOTH LocalConnectors mounted at the
    same VFS root, sys_readdir observation writes sharedzone metastore
    rows for every entry each side's LocalConnector.list_dir returns.
    Raft replicates, so `ls /shared/cc-tasks/` on either side returns
    the union — no per-hostname subtree traversal needed.

    Three steps with hard causal flow:
      1. Each side writes to its OWN host-fs `/host/tasks/<sess>/1.json`.
      2. Trigger sys_readdir on BOTH sides (via FUSE `ls`) so observation
         seeds metastore for each side's own entries.
      3. Assert BOTH sessions appear in `metastore.list` on BOTH nodes.
    """

    def test_both_sides_see_merged_uuid_list(
        self,
        topology: CcTasksTopology,
        api_key: str,
        ready_stack: None,
    ) -> None:
        founder_sess = _new_session()
        joiner_sess = _new_session()
        founder_payload = b'{"id":"F","writer":"founder-host-fs","peer_shared":true}'
        joiner_payload = b'{"id":"J","writer":"joiner-host-fs","peer_shared":true}'

        # Step 1: each side writes to its own container's host fs
        cc_tasks_share_helpers.host_task_write(
            topology.founder_container, f"/{founder_sess}/1.json", founder_payload
        )
        cc_tasks_share_helpers.host_task_write(
            topology.joiner_container, f"/{joiner_sess}/1.json", joiner_payload
        )
        try:
            # Step 2: trigger sys_readdir on both sides (FUSE ls) so
            # each side's LocalConnector.list_dir results get observed
            # into metastore.
            _ = cc_tasks_share_helpers.mount_listdir(
                topology.founder_container, topology.fuse_mount_point
            )
            _ = cc_tasks_share_helpers.mount_listdir(
                topology.joiner_container, topology.fuse_mount_point
            )

            # Step 3: assert both session UUIDs appear on BOTH nodes'
            # merged view via FUSE mount.  Poll with a bounded budget —
            # raft replication of the observation writes is ~ms but
            # not instantaneous.
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                founder_entries = cc_tasks_share_helpers.mount_listdir(
                    topology.founder_container, topology.fuse_mount_point
                )
                joiner_entries = cc_tasks_share_helpers.mount_listdir(
                    topology.joiner_container, topology.fuse_mount_point
                )
                if (
                    founder_sess in founder_entries
                    and joiner_sess in founder_entries
                    and founder_sess in joiner_entries
                    and joiner_sess in joiner_entries
                ):
                    break
                time.sleep(0.5)
            else:
                pytest.fail(
                    f"merged view never converged within 30s — "
                    f"founder sees: {founder_entries}; joiner sees: {joiner_entries}; "
                    f"expected both sides to include {founder_sess} AND {joiner_sess}"
                )
        finally:
            runbook_helpers.docker_exec(
                topology.founder_container,
                ["rm", "-rf", f"/host/tasks/{founder_sess}"],
                check=False,
            )
            runbook_helpers.docker_exec(
                topology.joiner_container,
                ["rm", "-rf", f"/host/tasks/{joiner_sess}"],
                check=False,
            )


# ---------------------------------------------------------------------------
# Workflow 2: cross-node byte-exact reads via try_remote_fetch.
# ---------------------------------------------------------------------------
class TestPeerSharedCrossNodeByteExact:
    """Each side reads OTHER's file byte-exact via try_remote_fetch.

    Peer-shared invariant: when node A's FUSE cat lands on a path whose
    metastore row's `last_writer_address` is node B, try_remote_fetch
    dispatches to B's `KernelBlobFetcher::read` → B's LocalConnector →
    B's host fs.  Same routing pattern the namespaced suite validates
    for founder→joiner + joiner→founder read; the differentiator here
    is that both writes target the SAME VFS subtree.

    Three steps:
      1. Each side writes to host fs + triggers observation.
      2. Founder cats joiner's file via FUSE → byte-exact from joiner.
      3. Joiner cats founder's file via FUSE → byte-exact from founder.
    """

    def test_each_side_reads_other_side_bytes_via_peer_fetch(
        self,
        topology: CcTasksTopology,
        api_key: str,
        ready_stack: None,
    ) -> None:
        founder_sess = _new_session()
        joiner_sess = _new_session()
        founder_bytes = b'{"writer":"founder","target":"cross-node-read"}'
        joiner_bytes = b'{"writer":"joiner","target":"cross-node-read"}'

        cc_tasks_share_helpers.host_task_write(
            topology.founder_container, f"/{founder_sess}/a.json", founder_bytes
        )
        cc_tasks_share_helpers.host_task_write(
            topology.joiner_container, f"/{joiner_sess}/b.json", joiner_bytes
        )
        try:
            # Trigger observation on BOTH sides so the metastore
            # rows exist for the cross-node reads.
            _ = cc_tasks_share_helpers.mount_listdir(
                topology.founder_container, topology.fuse_mount_point
            )
            _ = cc_tasks_share_helpers.mount_listdir(
                topology.joiner_container, topology.fuse_mount_point
            )
            # Nested descent triggers each session dir's inner readdir
            # observation — needed for the actual file entries to
            # replicate through sharedzone metastore.
            for grpc_pair in (
                (topology.founder_container, founder_sess),
                (topology.founder_container, joiner_sess),
                (topology.joiner_container, founder_sess),
                (topology.joiner_container, joiner_sess),
            ):
                cont, sess = grpc_pair
                try:
                    runbook_helpers.docker_exec(
                        cont, ["ls", f"{topology.fuse_mount_point}/{sess}"], check=False
                    )
                except Exception:  # noqa: BLE001
                    pass

            # Step 2: founder reads joiner's file via FUSE
            joiner_file_on_founder = f"{topology.fuse_mount_point}/{joiner_sess}/b.json"
            _poll_for_bytes(
                topology.founder_container,
                joiner_file_on_founder,
                joiner_bytes,
                "founder never got joiner's bytes via peer-fetch",
            )

            # Step 3: joiner reads founder's file via FUSE
            founder_file_on_joiner = f"{topology.fuse_mount_point}/{founder_sess}/a.json"
            _poll_for_bytes(
                topology.joiner_container,
                founder_file_on_joiner,
                founder_bytes,
                "joiner never got founder's bytes via peer-fetch",
            )
        finally:
            runbook_helpers.docker_exec(
                topology.founder_container,
                ["rm", "-rf", f"/host/tasks/{founder_sess}"],
                check=False,
            )
            runbook_helpers.docker_exec(
                topology.joiner_container,
                ["rm", "-rf", f"/host/tasks/{joiner_sess}"],
                check=False,
            )
