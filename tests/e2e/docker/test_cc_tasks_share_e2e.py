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
    """Gate on the daemon's `--mount-driver` loop having installed the mount."""
    runbook_helpers.wait_healthy([topology.founder_grpc])
    # `vfs_stat` enters through root zone (zone_id default), so the
    # routing exercises the LocalConnector mount at /tasks the
    # `--mount-driver` loop installs.  `found=False` is fine — what
    # we gate on is the absence of a routing error.  An `"error"`
    # response is the wedged-path signal that the mount hasn't
    # landed yet.
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
        f"LocalConnector mount never became reachable on {topology.founder_grpc} "
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


# ---------------------------------------------------------------------------
# Cross-node lazy-observe workflow
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def joined_cluster(topology: CcTasksTopology) -> dict:
    """Run the runbook §3b offline `nexusd-cluster join` once per module.

    Mirrors the federation-runbook suite's `joined_cluster` fixture
    but targets the cc-tasks-share two-node setup.  The joiner can't
    self-join (the static-only env-var matrix forbids
    `NEXUS_FEDERATION_ZONES` on restart), so we:

      1. Wait for founder to bootstrap sharedzone.
      2. Stop the joiner (release the redb file lock).
      3. Run `nexusd-cluster join` in a transient sidecar against
         the joiner's data volume.
      4. Start the joiner — entrypoint auto-detects restart mode
         and the apply-cb replay path wires the DT_MOUNT entries.

    The returned dict carries the join CLI stdout/stderr for
    diagnostic asserts; tests that just need the joined cluster pass
    `_=joined_cluster` to depend on it.
    """
    runbook_helpers.wait_healthy([topology.founder_grpc, topology.joiner_grpc])

    # Confirm founder's sharedzone is leader-elected before joiner asks
    # to join — otherwise JoinZone hits "not leader" and retries.
    runbook_helpers.wait_zone_ready(topology.founder_grpc, "sharedzone")

    founder_node_id = runbook_helpers.fetch_node_id(topology.founder_container)

    runbook_helpers.docker_stop(topology.joiner_container)
    try:
        join_proc = runbook_helpers.run_nexusd_cluster_join(
            target_container=topology.joiner_container,
            target_volume=f"{topology.joiner_container}-data",
            founder_node_id=founder_node_id,
            founder_addr=topology.founder_grpc,
            zone_id="sharedzone",
            local_path="/shared",
            hostname="joiner",
            cluster_image=os.environ.get(
                "NEXUS_CLUSTER_IMAGE", "nexus-local-connector-plugin:latest"
            ),
            network=os.environ.get("NEXUS_CC_TASKS_NETWORK", "nexus-cc-tasks-net"),
        )
    finally:
        runbook_helpers.docker_start(topology.joiner_container)

    runbook_helpers.wait_healthy([topology.joiner_grpc])

    return {
        "founder_node_id": founder_node_id,
        "join_stdout": join_proc.stdout.decode(errors="replace") if join_proc.stdout else "",
        "join_stderr": join_proc.stderr.decode(errors="replace") if join_proc.stderr else "",
    }


class TestCrossNodeLazyMaterialization:
    """The real cc-tasks-share workflow — Claude Code writes its own
    tasks to host fs, peer sees them via federation-routed reads.

    This pins the lazy-observe substrate landed in nexus-vfs#46.  The
    test is shaped as one long four-step workflow per the
    integration-test-generator spec: each step has a *necessary* data
    dependency on the prior step's output, no arbitrary calls.

    Workflow:
      1. CC on founder writes a task JSON directly to host fs (no
         Nexus syscall).  Output: bytes physically at
         `/host/tasks/<rel>` on founder; no Nexus metadata.
      2. Joiner does its first `vfs_read` for the same path.  The
         metastore miss + joiner's local backend miss should trigger
         the new fan-out arm: ask founder via `peer_client.fetch`.
         Output: the founder bytes return to the joiner caller.
         Required by step 3: this is what fires `observe` on
         founder's `BlobFetcher` and materialises metadata.
      3. Wait for raft replication of the metadata back to joiner —
         step 2's `observe` was the cause; this is the effect.
         Output: `vfs_stat` on joiner reports `found=True` with
         `last_writer_address = founder`.  Required by step 4: the
         fast path needs this metadata to skip fan-out.
      4. Joiner's second `vfs_read`.  Must succeed and return the
         same bytes — but this time via the existing
         `try_remote_fetch` path rather than the cold fan-out.  We
         can't assert "no fan-out happened" without instrumentation,
         but byte-equality + metadata presence + bounded time pins
         the fast-path contract.
    """

    def test_founder_host_write_visible_on_joiner_via_lazy_observe(
        self,
        topology: CcTasksTopology,
        api_key: str,
        joined_cluster: dict,
    ) -> None:
        relpath = "/session-cross/1.json"
        payload = b'{"task":"cross-node-lazy-observe","source":"founder"}'

        # Step 1: CC on founder writes its task JSON directly to host fs.
        # No Nexus syscall — no metadata, no last_writer_address.
        cc_tasks_share_helpers.host_task_write(
            topology.founder_container, relpath, payload
        )

        # Sanity: bytes really are on founder's host fs.  This pins
        # the "no Nexus involvement" precondition that defines the
        # workflow.
        founder_host_bytes = cc_tasks_share_helpers.host_task_read(
            topology.founder_container, relpath
        )
        assert founder_host_bytes == payload, (
            "host-fs direct write to founder didn't land — workflow "
            "precondition failed"
        )

        founder_path = topology.founder_vfs_path(relpath)

        # Step 2: joiner's first read — fan-out path.  Cold.  The
        # substrate fans out via `DistributedCoordinator::zone_peers`
        # → founder's `BlobFetcher::read` hits LocalConnector → bytes
        # come back AND `observe_backend_content` proposes metadata.
        first_read = runbook_helpers.vfs_read(
            topology.joiner_grpc, founder_path, api_key=api_key, timeout=30
        )
        assert "error" not in first_read, (
            f"joiner first vfs_read failed — fan-out broken. result={first_read}"
        )
        first_bytes = runbook_helpers.decode_content(first_read)
        assert first_bytes == payload, (
            f"joiner saw {first_bytes!r}, expected {payload!r} — fan-out "
            "returned wrong content"
        )

        # Step 3: wait for raft to replicate the metadata that
        # founder's `observe` proposed.  This is the verifiable
        # side-effect of step 2 — the contract says the peer-served
        # ctx fires observe → metadata propose.  Without it, step 4
        # would re-fan-out instead of taking the fast path.
        runbook_helpers.wait_nodes_caught_up(
            [topology.founder_grpc, topology.joiner_grpc],
            "sharedzone",
            api_key=api_key,
            timeout=30,
            probe_path=founder_path,
        )
        stat_after = runbook_helpers.vfs_stat(
            topology.joiner_grpc, founder_path, api_key=api_key, timeout=10
        )
        assert "error" not in stat_after, f"vfs_stat after replication failed: {stat_after}"
        stat_result = stat_after.get("result") or {}
        assert stat_result.get("found"), (
            f"joiner metadata not materialised after fan-out — observe never "
            f"fired or raft never replicated. stat={stat_after}"
        )
        # last_writer_address is the routing-pointer SSOT for cross-node
        # fetches.  After step 2, it must point at founder — that's the
        # node whose `BlobFetcher` synthesised the peer-served ctx and
        # called `observe_backend_content`.
        last_writer = stat_result.get("lastWriterAddress") or ""
        assert "founder" in last_writer, (
            f"last_writer_address should point at founder, got {last_writer!r}. "
            "Either observe ran on the wrong node or self_address wasn't wired."
        )

        # Step 4: second read — fast path via `try_remote_fetch` +
        # `last_writer_address` routing.  Byte-equality pins the
        # fast path serves the same content; the fact that we got a
        # `found=True` metadata in step 3 means we'll skip fan-out.
        second_read = runbook_helpers.vfs_read(
            topology.joiner_grpc, founder_path, api_key=api_key, timeout=10
        )
        assert "error" not in second_read, (
            f"joiner second vfs_read failed — fast path broken. result={second_read}"
        )
        second_bytes = runbook_helpers.decode_content(second_read)
        assert second_bytes == payload, (
            f"joiner second read got {second_bytes!r}, expected {payload!r} — "
            "metadata-driven fetch returned wrong content"
        )

    def test_joiner_host_write_visible_on_founder_via_lazy_observe(
        self,
        topology: CcTasksTopology,
        api_key: str,
        joined_cluster: dict,
    ) -> None:
        """Symmetric workflow — joiner writes, founder reads.

        Different path namespace (`/shared/cc-tasks/joiner/...`) so
        the LocalConnector instances at each end never get confused
        about who owns the bytes.  The substrate must work in both
        directions identically; without this test the previous
        could pass by accident (e.g. if founder were special-cased
        as the leader in the fan-out path).
        """
        relpath = "/session-cross-rev/1.json"
        payload = b'{"task":"cross-node-reverse","source":"joiner"}'

        cc_tasks_share_helpers.host_task_write(
            topology.joiner_container, relpath, payload
        )

        joiner_path = topology.joiner_vfs_path(relpath)

        first_read = runbook_helpers.vfs_read(
            topology.founder_grpc, joiner_path, api_key=api_key, timeout=30
        )
        assert "error" not in first_read, (
            f"founder first vfs_read failed — reverse fan-out broken. "
            f"result={first_read}"
        )
        assert runbook_helpers.decode_content(first_read) == payload

        runbook_helpers.wait_nodes_caught_up(
            [topology.founder_grpc, topology.joiner_grpc],
            "sharedzone",
            api_key=api_key,
            timeout=30,
            probe_path=joiner_path,
        )
        stat_after = runbook_helpers.vfs_stat(
            topology.founder_grpc, joiner_path, api_key=api_key, timeout=10
        )
        last_writer = (stat_after.get("result") or {}).get("lastWriterAddress") or ""
        assert "joiner" in last_writer, (
            f"reverse direction: last_writer_address should point at joiner, "
            f"got {last_writer!r}"
        )

        second_read = runbook_helpers.vfs_read(
            topology.founder_grpc, joiner_path, api_key=api_key, timeout=10
        )
        assert "error" not in second_read
        assert runbook_helpers.decode_content(second_read) == payload
