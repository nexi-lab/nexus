"""cc-tasks-share Docker E2E — full LocalConnector + FUSE + federation chain.

Locks down the operator-facing stack end-to-end on a two-node cluster
sharing a ``sharedzone``.  Each node carries three plugin layers
loaded by ``nexusd-cluster`` from ``--plugin-dir /plugins``:

  * ``libnexus_local_connector.so`` — driver dylib mounted via
    ``--mount-driver local-connector:sharedzone:/shared/cc-tasks/<host>:{local_root:/host/tasks}``,
    so host-fs writes at ``/host/tasks/`` become observable through the
    federated VFS path ``/shared/cc-tasks/<host>/``.
  * ``libnexus_fuse_plugin.so`` — service dylib spawning a fuser
    event loop that publishes ``/shared/cc-tasks`` back as a real OS
    mount at ``/mnt/cc-tasks``.  ``cat /mnt/cc-tasks/founder/abc/1.json``
    on the joiner walks the entire chain: FUSE driver → plugin event
    loop → KernelHandle v3 callbacks → DT_MOUNT routing → federation
    fan-out → founder's LocalConnector → founder's host fs.
  * the cluster's own substrate — raft-replicated zone state +
    lazy-observe metadata propose so the second cross-node read takes
    the fast path.

Test workflows are arranged per the integration-test-generator
standard: 3+ steps with hard causal data flow between them, mirroring
the real operator scenario (Claude Code on Mac writes a task JSON;
Claude Code on Windows runs ``cc tasks list`` and sees it; both sides
walk the same plugin chain).  Each workflow uses a uuid4-namespaced
session directory + ``try/finally`` cleanup so concurrent runs (xdist)
don't trip over shared paths.

The "share" goal — Mac↔Win VPN topology — is verified manually per
``docs/architecture/federation-cross-machine-runbook.md`` once this
suite is green; this E2E is the regression guard before that smoke.
"""

from __future__ import annotations

import concurrent.futures
import os
import time
import uuid

import pytest

from tests.e2e.docker import cc_tasks_share_helpers, runbook_helpers
from tests.e2e.docker.cc_tasks_share_helpers import CcTasksTopology

# Skip when not invoked from the dedicated workflow that brings the
# cluster up — the compose file's `test` service sets these.
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
    """Gate on the daemon's `--mount-driver` loop having installed the mount.

    Two gates: (a) the LocalConnector mount at ``/shared/cc-tasks/founder``
    answers ``vfs_stat`` without a routing error; (b) the FUSE plugin
    has finished ``fuser::spawn_mount2`` so ``/mnt/cc-tasks`` is a real
    mount point.  The compose's healthcheck guards (b) for both nodes,
    but we keep a polling backstop here for the rare race where the
    test container observes "healthy" the instant before the mount
    point appears.
    """
    runbook_helpers.wait_healthy([topology.founder_grpc])
    deadline = time.time() + 60
    while time.time() < deadline:
        stat = runbook_helpers.vfs_stat(
            topology.founder_grpc,
            topology.local_connector_vfs_root,
            api_key=api_key,
            timeout=5,
        )
        if "error" not in stat and cc_tasks_share_helpers.mount_is_fuse_mountpoint(
            topology.founder_container, topology.fuse_mount_point
        ):
            return
        time.sleep(0.5)
    pytest.fail(
        f"founder substrate never became reachable on {topology.founder_grpc} within 60s — "
        f"check --mount-driver and FUSE plugin boot logs"
    )


def _wait_path_via_grpc(
    topology: CcTasksTopology,
    grpc: str,
    path: str,
    api_key: str,
    *,
    expect_found: bool,
    timeout: float = 10.0,
) -> dict:
    """Poll vfs_stat until path matches expected presence.

    Mount-side ops complete synchronously inside the container but
    metastore writes can race the test runner's next gRPC call by a
    millisecond or two.  A 10 s budget with 100 ms polls absorbs that
    race without masking real bugs.
    """
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = runbook_helpers.vfs_stat(grpc, path, api_key=api_key, timeout=5)
        if "error" not in last and last.get("result", {}).get("found") == expect_found:
            return last
        time.sleep(0.1)
    pytest.fail(
        f"vfs_stat({path}) on {grpc} never reached expected found={expect_found} "
        f"within {timeout}s — last result: {last}"
    )


def _new_session() -> str:
    """uuid4 session name — keeps parallel runs from colliding on paths."""
    return f"session-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Workflow 1: stack readiness — every plugin layer up on every node.
# ---------------------------------------------------------------------------
class TestStackReadiness:
    """Sanity gate: cluster + LocalConnector + FUSE all up on both nodes.

    Three steps because the stack has three layers; each step depends
    on the prior — without (1) we can't probe (2), and without (2) the
    FUSE plugin's create-time bind to ``/shared/cc-tasks`` would have
    failed.
    """

    def test_full_stack_responds_on_both_nodes(
        self, topology: CcTasksTopology, api_key: str, ready_node: None
    ) -> None:
        # Step 1: gRPC reachable on both nodes — daemons booted.
        for grpc in (topology.founder_grpc, topology.joiner_grpc):
            stat = runbook_helpers.vfs_stat(grpc, "/", api_key=api_key, timeout=5)
            assert "error" not in stat, f"gRPC vfs_stat(/) failed on {grpc}: {stat}"
            assert stat["result"]["found"], "/ must exist on a fresh cluster"

        # Step 2: LocalConnector mount answers vfs_stat on each node's
        # own namespace — the `--mount-driver` loop completed.
        founder_root_stat = runbook_helpers.vfs_stat(
            topology.founder_grpc,
            topology.founder_vfs_path("/"),
            api_key=api_key,
            timeout=5,
        )
        assert "error" not in founder_root_stat, (
            f"founder LocalConnector mount missing: {founder_root_stat}"
        )
        joiner_root_stat = runbook_helpers.vfs_stat(
            topology.joiner_grpc,
            topology.joiner_vfs_path("/"),
            api_key=api_key,
            timeout=5,
        )
        assert "error" not in joiner_root_stat, (
            f"joiner LocalConnector mount missing: {joiner_root_stat}"
        )

        # Step 3: FUSE plugin finished spawn_mount2 on both nodes —
        # depends on (2) because the plugin binds to /shared/cc-tasks
        # at create-time and would refuse to spawn the event loop if
        # the path weren't routable.
        for container in (topology.founder_container, topology.joiner_container):
            assert cc_tasks_share_helpers.mount_is_fuse_mountpoint(
                container, topology.fuse_mount_point
            ), (
                f"{topology.fuse_mount_point} on {container} is not a FUSE mount — "
                f"plugin's `fuser::spawn_mount2` did not complete.  Check the "
                f"cluster's stderr for the `FUSE mount failed` log line."
            )


# ---------------------------------------------------------------------------
# Workflow 2: single-node operator-surface chain (host fs ↔ gRPC ↔ FUSE).
# ---------------------------------------------------------------------------
class TestSingleNodeOperatorSurface:
    """Bytes round-trip through every surface a single node exposes.

    Five-step workflow mirroring the operator's day-1 path:

      1. CC drops a task JSON in ``~/.claude/tasks/`` — host fs write.
      2. ``vfs_read`` over gRPC sees those bytes — LocalConnector
         backend resolves the path.  Required by step 3: confirms the
         metadata path exists before we exercise FUSE which composes
         on top of it.
      3. ``cat /mnt/cc-tasks/founder/<rel>`` via FUSE returns the same
         bytes — the FUSE event loop's read callback walks all the
         way down to the LocalConnector.
      4. CC updates the JSON via the FUSE mount (``cat > <path>``).
         Output: the FUSE write callback fires sys_write through
         KernelHandle v3.  Required by step 5.
      5. ``/host/tasks/<rel>`` shows the new bytes — SSOT preserved
         end-to-end.  A regression in FUSE's write callback or the
         LocalConnector's write impl breaks this.
    """

    def test_host_write_then_grpc_then_fuse_then_write_back(
        self, topology: CcTasksTopology, api_key: str, ready_node: None
    ) -> None:
        session = _new_session()
        relpath = f"/{session}/1.json"
        initial = b'{"task":"first","step":"host-write"}'
        updated = b'{"task":"first","step":"fuse-overwrite"}'
        vfs_path = topology.founder_vfs_path(relpath)
        mount_path = topology.mount_path_for_vfs(vfs_path)

        try:
            # Step 1: host-fs write (Claude Code analogue).
            cc_tasks_share_helpers.host_task_write(topology.founder_container, relpath, initial)

            # Step 2: gRPC vfs_read sees the bytes via LocalConnector.
            grpc_read = runbook_helpers.vfs_read(topology.founder_grpc, vfs_path, api_key=api_key)
            assert "error" not in grpc_read, f"gRPC vfs_read failed: {grpc_read}"
            assert runbook_helpers.decode_content(grpc_read) == initial, (
                "LocalConnector backend did not surface host-fs bytes via gRPC — "
                "SSOT property broken at the gRPC surface"
            )

            # Step 3: FUSE mount read returns the same bytes —
            # the chain FUSE → kernel → DT_MOUNT → LocalConnector → host fs.
            fuse_read = cc_tasks_share_helpers.mount_read_bytes(
                topology.founder_container, mount_path
            )
            assert fuse_read == initial, (
                f"FUSE mount surfaced {fuse_read!r}, expected {initial!r} — "
                "either FUSE read callback or downstream routing dropped bytes"
            )

            # Step 4: overwrite via FUSE mount — exercises the write
            # callback through KernelHandle v3.
            cc_tasks_share_helpers.mount_write_bytes(
                topology.founder_container, mount_path, updated
            )

            # Step 5: host fs shows the new bytes — SSOT preserved
            # through the full stack.
            after_bytes = cc_tasks_share_helpers.host_task_read(topology.founder_container, relpath)
            assert after_bytes == updated, (
                f"host fs has {after_bytes!r}, expected {updated!r} — "
                "FUSE write callback or LocalConnector write impl dropped bytes"
            )
        finally:
            # Cleanup: rm via host fs (FUSE may not be up at teardown).
            runbook_helpers.docker_exec(
                topology.founder_container,
                ["rm", "-rf", f"/host/tasks/{session}"],
                check=False,
            )


# ---------------------------------------------------------------------------
# Workflow 3: full session lifecycle through the FUSE mount.
# ---------------------------------------------------------------------------
class TestSessionLifecycleViaFuse:
    """One session's worth of operator ops, all through the FUSE mount.

    Seven-step workflow exercises every dir-mutating FUSE callback the
    KernelHandle v3 plugin wires.  The data flow is sequential — step
    N can't observe correct state unless step N-1 actually did what
    its assertion claimed.
    """

    def test_mkdir_write_ls_read_rename_unlink_rmdir(
        self, topology: CcTasksTopology, api_key: str, ready_node: None
    ) -> None:
        session = _new_session()
        session_vfs = topology.founder_vfs_path(f"/{session}")
        session_mount = topology.mount_path_for_vfs(session_vfs)

        files = [(f"{i}.json", f'{{"task":{i},"session":"{session}"}}'.encode()) for i in range(3)]

        try:
            # Step 1: mkdir session dir via FUSE.
            cc_tasks_share_helpers.mount_mkdir(topology.founder_container, session_mount)
            stat = _wait_path_via_grpc(
                topology,
                topology.founder_grpc,
                session_vfs,
                api_key,
                expect_found=True,
            )
            assert stat["result"]["isDirectory"], (
                "mkdir via FUSE landed as non-directory in kernel metastore — "
                "sys_mkdir callback wired wrong entry_type"
            )

            # Step 2: write N task JSONs via FUSE.  Depends on (1).
            for name, payload in files:
                cc_tasks_share_helpers.mount_write_bytes(
                    topology.founder_container, f"{session_mount}/{name}", payload
                )

            # Step 3: ls via FUSE returns all N entries.  Depends on (2)
            # because readdir must see what write created.
            entries = cc_tasks_share_helpers.mount_listdir(
                topology.founder_container, session_mount
            )
            expected = sorted(name for name, _ in files)
            assert entries == expected, (
                f"readdir via FUSE returned {entries}, expected {expected} — "
                "sys_readdir callback missed entries written via sys_write"
            )

            # Step 4: read each file byte-exact via FUSE.  Depends on
            # (2) because content presence is the write's effect.
            for name, payload in files:
                roundtrip = cc_tasks_share_helpers.mount_read_bytes(
                    topology.founder_container, f"{session_mount}/{name}"
                )
                assert roundtrip == payload, (
                    f"FUSE read of {name} returned {roundtrip!r}, expected {payload!r}"
                )

            # Step 5: rename one file via FUSE.  Depends on (2).
            old_name, payload = files[0]
            new_name = "renamed.json"
            cc_tasks_share_helpers.mount_rename(
                topology.founder_container,
                f"{session_mount}/{old_name}",
                f"{session_mount}/{new_name}",
            )
            # Old path absent + new path present + content preserved.
            _wait_path_via_grpc(
                topology,
                topology.founder_grpc,
                f"{session_vfs}/{old_name}",
                api_key,
                expect_found=False,
            )
            _wait_path_via_grpc(
                topology,
                topology.founder_grpc,
                f"{session_vfs}/{new_name}",
                api_key,
                expect_found=True,
            )
            renamed_bytes = cc_tasks_share_helpers.mount_read_bytes(
                topology.founder_container, f"{session_mount}/{new_name}"
            )
            assert renamed_bytes == payload, (
                "rename via FUSE preserved path but lost bytes — sys_rename "
                "moved metadata but lost content reference"
            )

            # Step 6: unlink every file via FUSE.  Depends on (5)
            # because we need the renamed name in the unlink set.
            remaining = [(new_name, payload)] + [(n, p) for n, p in files[1:]]
            for name, _ in remaining:
                cc_tasks_share_helpers.mount_unlink(
                    topology.founder_container, f"{session_mount}/{name}"
                )
            assert (
                cc_tasks_share_helpers.mount_listdir(topology.founder_container, session_mount)
                == []
            ), "directory not empty after sys_unlink batch — unlink callback dropped one"

            # Step 7: rmdir the now-empty session dir.  Depends on (6).
            cc_tasks_share_helpers.mount_rmdir(topology.founder_container, session_mount)
            _wait_path_via_grpc(
                topology,
                topology.founder_grpc,
                session_vfs,
                api_key,
                expect_found=False,
            )
        finally:
            # Belt-and-suspenders: blow away any residue via host fs.
            runbook_helpers.docker_exec(
                topology.founder_container,
                ["rm", "-rf", f"/host/tasks/{session}"],
                check=False,
            )


# ---------------------------------------------------------------------------
# Workflow 4: symlink escape rejected at every layer.
# ---------------------------------------------------------------------------
class TestSymlinkEscapeAtAllLayers:
    """A symlink pointing outside ``local_root`` stays rejected everywhere.

    Three-step workflow proves the LocalConnector's ``resolve_path``
    escape check fires regardless of which surface the read enters
    through.  A regression in the kernel's path-traversal lift or the
    FUSE plugin's read-translation could leak host bytes through one
    layer even if the LocalConnector itself is correct; this catches
    both.
    """

    def test_symlink_rejected_via_grpc_and_via_fuse(
        self, topology: CcTasksTopology, api_key: str, ready_node: None
    ) -> None:
        session = _new_session()
        link_relpath = f"/{session}/escape.json"
        vfs_path = topology.founder_vfs_path(link_relpath)
        mount_path = topology.mount_path_for_vfs(vfs_path)

        try:
            # Step 1: plant the symlink — `escape.json` → /etc/passwd.
            cc_tasks_share_helpers.host_task_symlink(
                topology.founder_container, link_relpath, "/etc/passwd"
            )

            # Step 2: gRPC vfs_read rejects.  PermissionDenied surfaces
            # as a gRPC error OR a non-matching content; either way no
            # /etc/passwd bytes leak.
            grpc_result = runbook_helpers.vfs_read(topology.founder_grpc, vfs_path, api_key=api_key)
            if "result" in grpc_result:
                content = grpc_result["result"].get("content", b"")
                assert b"root:" not in content, (
                    "symlink escape leaked /etc/passwd via gRPC vfs_read"
                )

            # Step 3: FUSE mount read rejects.  ``cat`` either fails
            # (rc != 0) or returns empty/non-passwd bytes — never the
            # /etc/passwd contents.  Direct subprocess so we capture
            # both rc and bytes without the helper's fail-on-rc gate.
            import subprocess

            proc = subprocess.run(
                ["docker", "exec", topology.founder_container, "cat", mount_path],
                capture_output=True,
                timeout=30,
            )
            if proc.returncode == 0:
                assert b"root:" not in proc.stdout, (
                    "symlink escape leaked /etc/passwd via FUSE mount"
                )
        finally:
            runbook_helpers.docker_exec(
                topology.founder_container,
                ["rm", "-rf", f"/host/tasks/{session}"],
                check=False,
            )


# ---------------------------------------------------------------------------
# Cross-node lazy-materialisation workflow — the real cc-tasks-share goal.
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
    # Cross-node tests fan out via `peer_client.fetch` using
    # `DistributedCoordinator::zone_peers(sharedzone)` — joiner needs
    # to be a full member (ConfState containing founder) before the
    # lookup returns a usable peer roster.  Without this gate the first
    # `vfs_read` after the fixture races joiner's post-restart raft
    # catchup and surfaces FileNotFound against an empty peer list.
    runbook_helpers.wait_zone_ready(topology.founder_grpc, "sharedzone")
    runbook_helpers.wait_zone_ready(topology.joiner_grpc, "sharedzone")

    # Also wait for joiner's FUSE mount to come back up after the
    # restart — the FUSE plugin re-spawns at startup.
    deadline = time.time() + 60
    while time.time() < deadline:
        if cc_tasks_share_helpers.mount_is_fuse_mountpoint(
            topology.joiner_container, topology.fuse_mount_point
        ):
            break
        time.sleep(0.5)
    else:
        pytest.fail("joiner FUSE mount did not come back up after join restart")

    return {
        "founder_node_id": founder_node_id,
        "join_stdout": join_proc.stdout or "",
        "join_stderr": join_proc.stderr or "",
    }


class TestCrossNodeFuseFederationWorkflow:
    """Mac↔Win ``cc tasks list`` analogue — full chain through FUSE+fed.

    Six-step workflow flows founder→joiner→founder via the operator-
    facing FUSE mount surface end-to-end.  Pins both the lazy-observe
    substrate (nexus-vfs#46) AND the FUSE plugin's correctness when
    its reads cross a federation boundary.

    The substrate without FUSE was already covered by the prior
    ``TestCrossNodeLazyMaterialization`` (now folded in here);
    the FUSE layer on top is the operator-facing chain we ship.
    """

    def test_founder_host_write_visible_via_joiner_fuse_then_symmetric(
        self,
        topology: CcTasksTopology,
        api_key: str,
        joined_cluster: dict,
    ) -> None:
        session = _new_session()
        relpath = f"/{session}/1.json"
        founder_payload = (
            b'{"task":"cross-node-fuse","source":"founder","session":"' + session.encode() + b'"}'
        )
        founder_vfs = topology.founder_vfs_path(relpath)
        founder_mount = topology.mount_path_for_vfs(founder_vfs)

        try:
            # Step 1: Claude Code on founder writes a task JSON
            # directly to host fs.  No Nexus syscall — no metadata,
            # no last_writer_address.
            cc_tasks_share_helpers.host_task_write(
                topology.founder_container, relpath, founder_payload
            )

            # Step 2: sanity — bytes are physically on founder's host
            # fs.  Pins the "no Nexus involvement" precondition.
            sanity = cc_tasks_share_helpers.host_task_read(topology.founder_container, relpath)
            assert sanity == founder_payload, (
                "host-fs direct write to founder didn't land — workflow precondition failed"
            )

            # Step 3: joiner's first FUSE read — the whole chain end
            # to end.  `cat /mnt/cc-tasks/founder/<rel>` flows
            # FUSE driver → plugin event loop → sys_read → DT_MOUNT
            # routing → federation fan-out via peer_client.fetch →
            # founder's BlobFetcher → founder's LocalConnector →
            # founder's host fs.  Bytes return AND observe fires.
            joiner_fuse_first = cc_tasks_share_helpers.mount_read_bytes(
                topology.joiner_container, founder_mount
            )
            assert joiner_fuse_first == founder_payload, (
                f"joiner FUSE first-read saw {joiner_fuse_first!r}, expected "
                f"{founder_payload!r} — cross-node fan-out via FUSE broken"
            )

            # Step 4: wait for raft to replicate metadata that founder's
            # `observe` proposed during step 3.  Required by step 5:
            # the fast path needs last_writer_address.
            runbook_helpers.wait_nodes_caught_up(
                [topology.founder_grpc, topology.joiner_grpc],
                "sharedzone",
                api_key=api_key,
                timeout=30,
                probe_path=founder_vfs,
            )
            stat_after = runbook_helpers.vfs_stat(
                topology.joiner_grpc, founder_vfs, api_key=api_key, timeout=10
            )
            stat_result = stat_after.get("result") or {}
            assert stat_result.get("found"), (
                f"joiner metadata not materialised after fan-out — observe never "
                f"fired or raft never replicated. stat={stat_after}"
            )
            last_writer = stat_result.get("lastWriterAddress") or ""
            assert "founder" in last_writer, (
                f"last_writer_address should point at founder, got {last_writer!r}. "
                "Either observe ran on the wrong node or self_address wasn't wired."
            )

            # Step 5: joiner's second FUSE read — fast path via
            # try_remote_fetch + last_writer_address.  Same bytes, but
            # routed differently inside the kernel.
            joiner_fuse_second = cc_tasks_share_helpers.mount_read_bytes(
                topology.joiner_container, founder_mount
            )
            assert joiner_fuse_second == founder_payload, (
                f"joiner FUSE fast-path read saw {joiner_fuse_second!r}, expected "
                f"{founder_payload!r} — metadata-driven fetch broken"
            )

            # Step 6: symmetric — joiner writes via FUSE, founder reads
            # via FUSE.  Same chain in reverse: `cat > /mnt/cc-tasks/
            # joiner/<rel>` on joiner triggers LocalConnector write +
            # observe; founder reads via FUSE → cross-node fan-out →
            # joiner's bytes.
            rev_relpath = f"/{session}/rev.json"
            rev_payload = b'{"task":"cross-node-fuse","source":"joiner"}'
            joiner_vfs = topology.joiner_vfs_path(rev_relpath)
            joiner_mount = topology.mount_path_for_vfs(joiner_vfs)

            cc_tasks_share_helpers.mount_write_bytes(
                topology.joiner_container, joiner_mount, rev_payload
            )

            # Sanity for reverse: bytes on joiner's host fs.  FUSE
            # write → kernel sys_write → LocalConnector → host fs.
            joiner_host_bytes = cc_tasks_share_helpers.host_task_read(
                topology.joiner_container, rev_relpath
            )
            assert joiner_host_bytes == rev_payload, (
                f"joiner FUSE write did not reach host fs: got {joiner_host_bytes!r}"
            )

            # Founder reads via its FUSE mount — cross-node fan-out
            # in the reverse direction.  No special-casing of founder
            # as leader: the substrate must work both ways identically.
            founder_fuse_read = cc_tasks_share_helpers.mount_read_bytes(
                topology.founder_container, joiner_mount
            )
            assert founder_fuse_read == rev_payload, (
                f"founder FUSE cross-node read got {founder_fuse_read!r}, "
                f"expected {rev_payload!r} — reverse-direction fan-out broken"
            )
        finally:
            for container in (topology.founder_container, topology.joiner_container):
                runbook_helpers.docker_exec(
                    container, ["rm", "-rf", f"/host/tasks/{session}"], check=False
                )


# ---------------------------------------------------------------------------
# Concurrency soak — operator burst-write pattern.
# ---------------------------------------------------------------------------
class TestCrossNodeConcurrentBurst:
    """N parallel FUSE writes on founder → joiner sees them all via FUSE.

    Four-step workflow exercises the Claude Code burst-write pattern
    (rapid task rewrites) end-to-end through the FUSE surface.  Each
    step verifies one stage of the fan-out lifecycle.
    """

    def test_concurrent_fuse_writes_become_visible_cross_node(
        self,
        topology: CcTasksTopology,
        api_key: str,
        joined_cluster: dict,
    ) -> None:
        session = _new_session()
        session_vfs = topology.founder_vfs_path(f"/{session}")
        session_mount_founder = topology.mount_path_for_vfs(session_vfs)
        session_mount_joiner = topology.mount_path_for_vfs(session_vfs)

        n = 6
        payloads = {f"{i}.json": f'{{"i":{i},"session":"{session}"}}'.encode() for i in range(n)}

        try:
            # Step 1: prep the session dir via FUSE.
            cc_tasks_share_helpers.mount_mkdir(topology.founder_container, session_mount_founder)

            # Step 2: burst-write via FUSE on founder.  Concurrency 4
            # mirrors the operator's worst case (Claude Code task
            # batch rewrite).
            def _write(name_payload: tuple[str, bytes]) -> None:
                name, payload = name_payload
                cc_tasks_share_helpers.mount_write_bytes(
                    topology.founder_container,
                    f"{session_mount_founder}/{name}",
                    payload,
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                for fut in concurrent.futures.as_completed(
                    [pool.submit(_write, item) for item in payloads.items()]
                ):
                    fut.result(timeout=30)

            # Step 3: every write visible via founder's gRPC vfs_stat —
            # the local sys_write side-effect chain is intact.
            for name in payloads:
                _wait_path_via_grpc(
                    topology,
                    topology.founder_grpc,
                    f"{session_vfs}/{name}",
                    api_key,
                    expect_found=True,
                )

            # Step 4: joiner's FUSE listdir + per-file read sees every
            # file byte-exact.  This is the cross-node end of the chain:
            # joiner's FUSE → kernel → federation routing → founder's
            # LocalConnector.  An eventual-consistency window is
            # natural here, so each file gets its own wait via gRPC
            # before the FUSE read.
            for name, payload in payloads.items():
                _wait_path_via_grpc(
                    topology,
                    topology.joiner_grpc,
                    f"{session_vfs}/{name}",
                    api_key,
                    expect_found=True,
                    timeout=30,
                )
                joiner_bytes = cc_tasks_share_helpers.mount_read_bytes(
                    topology.joiner_container,
                    f"{session_mount_joiner}/{name}",
                )
                assert joiner_bytes == payload, (
                    f"joiner FUSE cross-node read of {name} got {joiner_bytes!r}, "
                    f"expected {payload!r}"
                )

            # Also verify joiner's FUSE listdir surfaces every entry —
            # readdir over the federation boundary.
            joiner_entries = cc_tasks_share_helpers.mount_listdir(
                topology.joiner_container, session_mount_joiner
            )
            assert sorted(joiner_entries) == sorted(payloads.keys()), (
                f"joiner FUSE readdir returned {joiner_entries}, expected {sorted(payloads.keys())}"
            )
        finally:
            runbook_helpers.docker_exec(
                topology.founder_container,
                ["rm", "-rf", f"/host/tasks/{session}"],
                check=False,
            )
