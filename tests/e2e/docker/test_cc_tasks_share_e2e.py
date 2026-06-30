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
import subprocess
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
    """Gate on cluster boot + FUSE mount for both nodes; LocalConnector for founder.

    Three gates per node: (a) gRPC reachable, (b) FUSE plugin has
    finished ``fuser::spawn_mount2`` so ``/mnt/cc-tasks`` is a real
    mount point, (c) founder-only — the LocalConnector mount answers
    ``vfs_stat`` (founder's first boot installs it via
    ``NEXUS_FEDERATION_ZONES`` env).

    Joiner deliberately does **not** wait on the LocalConnector mount
    here: joiner's first boot logs ``skipping --mount-driver: target
    zone not loaded`` because ``sharedzone`` only lands after the
    runbook §3b offline join (the ``joined_cluster`` fixture).
    Tests that need joiner's LocalConnector chain (cross-node
    workflows + TestStackReadiness's step 2) MUST depend on
    ``joined_cluster``, which gates on joiner's post-join
    ``--mount-driver`` replay.  60 s budget matches the compose
    healthcheck's 24 retries × 5 s interval cap.
    """
    runbook_helpers.wait_healthy([topology.founder_grpc, topology.joiner_grpc])

    # Founder: full triplet — gRPC + LocalConnector + FUSE.
    deadline = time.time() + 60
    while time.time() < deadline:
        stat = runbook_helpers.vfs_stat(
            topology.founder_grpc,
            topology.founder_vfs_path("/"),
            api_key=api_key,
            timeout=5,
        )
        if "error" not in stat and cc_tasks_share_helpers.mount_is_fuse_mountpoint(
            topology.founder_container, topology.fuse_mount_point
        ):
            break
        time.sleep(0.5)
    else:
        pytest.fail(
            f"founder substrate never became reachable on {topology.founder_grpc} within 60s — "
            "check --mount-driver and FUSE plugin boot logs"
        )

    # Joiner: gRPC + FUSE only.  LocalConnector check is `joined_cluster`'s job.
    deadline = time.time() + 60
    while time.time() < deadline:
        root_stat = runbook_helpers.vfs_stat(topology.joiner_grpc, "/", api_key=api_key, timeout=5)
        if "error" not in root_stat and cc_tasks_share_helpers.mount_is_fuse_mountpoint(
            topology.joiner_container, topology.fuse_mount_point
        ):
            return
        time.sleep(0.5)
    pytest.fail(
        f"joiner substrate never became reachable on {topology.joiner_grpc} within 60s — "
        "gRPC up + FUSE mount mounted is what we need (LocalConnector waits for join)"
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

    Depends on ``joined_cluster`` because joiner's LocalConnector
    mount only lands after the runbook §3b offline join replays
    ``--mount-driver`` against the post-join data dir.  Asserting on
    joiner's LocalConnector before that would always fail.
    """

    def test_full_stack_responds_on_both_nodes(
        self,
        topology: CcTasksTopology,
        api_key: str,
        ready_node: None,
        joined_cluster: dict,
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

    One operator-facing workflow: write through Nexus (FUSE mount) →
    all surfaces (FUSE read, gRPC, host-fs) see the same bytes, and
    same-size host-fs overwrites stay visible through FUSE because
    the kernel metastore already has the entry and sys_read falls
    through to the LocalConnector backend on each call.
    """

    def test_fuse_write_visible_at_grpc_and_host_fs_then_host_overwrite_visible_via_fuse(
        self, topology: CcTasksTopology, api_key: str, ready_node: None
    ) -> None:
        """6-step: FUSE write materialises metadata + bytes; host overwrite stays visible.

        Once the path is materialised in the kernel metastore via FUSE
        write, subsequent host-fs overwrites (same-size payload) ARE
        visible via FUSE read — the metastore lookup hits, sys_read
        falls through to the LocalConnector backend, and the backend
        reads disk.  Same-size constraint reflects the
        materialised-once contract; size-mismatched overwrite would
        truncate at the cached metadata size.
        """
        session = _new_session()
        relpath = f"/{session}/1.json"
        initial = b'{"task":"first","step":"fuse-write-X"}'
        overwrite = b'{"task":"first","step":"host-rewrite"}'
        # Same byte length keeps the metastore-cached size honest
        # against the LocalConnector backend bytes.
        assert len(initial) == len(overwrite), (
            f"test bug: initial({len(initial)}) != overwrite({len(overwrite)})"
        )
        vfs_path = topology.founder_vfs_path(relpath)
        mount_path = topology.mount_path_for_vfs(vfs_path)

        try:
            # Step 1: FUSE write — sys_write fires, kernel materialises
            # metadata, LocalConnector writes through to host fs.
            # FUSE `cat >` does not auto-mkdir; create parent first.
            cc_tasks_share_helpers.mount_mkdir(
                topology.founder_container,
                topology.mount_path_for_vfs(topology.founder_vfs_path(f"/{session}")),
            )
            cc_tasks_share_helpers.mount_write_bytes(
                topology.founder_container, mount_path, initial
            )

            # Step 2: gRPC vfs_read sees the bytes.  Confirms the
            # sys_write reached the LocalConnector backend.
            grpc_read = runbook_helpers.vfs_read(topology.founder_grpc, vfs_path, api_key=api_key)
            assert "error" not in grpc_read, f"gRPC vfs_read failed: {grpc_read}"
            assert runbook_helpers.decode_content(grpc_read) == initial, (
                "FUSE-write bytes did not round-trip via gRPC vfs_read"
            )

            # Step 3: SSOT — host fs has the bytes.  LocalConnector is
            # not a copy; bytes live at /host/tasks/<rel> directly.
            host_bytes = cc_tasks_share_helpers.host_task_read(topology.founder_container, relpath)
            assert host_bytes == initial, (
                f"host fs has {host_bytes!r}, expected {initial!r} — "
                "FUSE write or LocalConnector write impl dropped bytes"
            )

            # Step 4: host-fs overwrite (Claude Code rewriting the JSON
            # in place).  Same-size payload preserves the metastore
            # size cache.
            cc_tasks_share_helpers.host_task_write(topology.founder_container, relpath, overwrite)

            # Step 5: gRPC vfs_read sees the new bytes — LocalConnector
            # backend re-reads from disk each call.
            grpc_after = runbook_helpers.vfs_read(topology.founder_grpc, vfs_path, api_key=api_key)
            assert "error" not in grpc_after, (
                f"gRPC vfs_read after host overwrite failed: {grpc_after}"
            )
            assert runbook_helpers.decode_content(grpc_after) == overwrite

            # Step 6: FUSE read also sees the new bytes — metadata
            # was materialised in step 1 so lookup succeeds; sys_read
            # falls through to the backend.
            fuse_after = cc_tasks_share_helpers.mount_read_bytes(
                topology.founder_container, mount_path
            )
            assert fuse_after == overwrite, (
                f"FUSE read after host overwrite got {fuse_after!r}, "
                f"expected {overwrite!r} — sys_read should fall through "
                "to LocalConnector backend regardless of metastore cache"
            )
        finally:
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

    Six-step workflow exercises the dir-mutating FUSE callbacks the
    KernelHandle v3 plugin wires AND the LocalConnector backend
    implements: mkdir, write (creates file), readdir, read, unlink,
    rmdir.  The FUSE adapter's ``rename`` callback itself is exercised
    in the WinFsp Windows E2E suite against an in-memory backend.
    """

    def test_mkdir_write_ls_read_unlink_rmdir(
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

            # Step 5: unlink every file via FUSE.  Depends on (2).
            for name, _ in files:
                cc_tasks_share_helpers.mount_unlink(
                    topology.founder_container, f"{session_mount}/{name}"
                )
            assert (
                cc_tasks_share_helpers.mount_listdir(topology.founder_container, session_mount)
                == []
            ), "directory not empty after sys_unlink batch — unlink callback dropped one"

            # Step 6: rmdir the now-empty session dir.  Depends on (5).
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
    facing FUSE mount surface end-to-end.  Pins the FUSE plugin's
    correctness when its reads cross a federation boundary AND the
    raft-replicated metadata + last_writer_address routing the joiner
    needs to fetch bytes from founder's host fs.

    Mode: writes go through the FUSE mount (not direct host fs) so
    sys_write materialises kernel metadata + last_writer_address.
    The Mac↔Win operator workflow's write-through-FUSE shape matches
    this — the FUSE mount IS the write surface, not just the read
    surface.
    """

    def test_founder_fuse_write_visible_via_joiner_fuse_then_symmetric(
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
            # Step 1: founder writes the task JSON via its FUSE mount.
            # `cat > /mnt/cc-tasks/founder/<rel>` triggers sys_write
            # which (a) writes bytes through LocalConnector to founder's
            # host fs and (b) materialises metadata in founder's
            # metastore with last_writer_address=founder.  Parent dir
            # is created first — FUSE `cat >` does not auto-mkdir.
            cc_tasks_share_helpers.mount_mkdir(
                topology.founder_container,
                topology.mount_path_for_vfs(topology.founder_vfs_path(f"/{session}")),
            )
            cc_tasks_share_helpers.mount_write_bytes(
                topology.founder_container, founder_mount, founder_payload
            )

            # Step 2: sanity — bytes are physically on founder's host
            # fs (SSOT) AND metastore has the entry.  Establishes the
            # workflow precondition both for SSOT and for raft replay.
            founder_host_bytes = cc_tasks_share_helpers.host_task_read(
                topology.founder_container, relpath
            )
            assert founder_host_bytes == founder_payload, (
                "founder FUSE write did not reach host fs — workflow precondition failed"
            )
            founder_stat = runbook_helpers.vfs_stat(
                topology.founder_grpc, founder_vfs, api_key=api_key
            )
            assert founder_stat["result"]["found"], (
                f"founder metadata missing after FUSE write — sys_write didn't "
                f"propose? stat={founder_stat}"
            )

            # Step 3: wait for raft to replicate metadata to joiner.
            # Required by steps 4+5: joiner's FUSE lookup needs the
            # metastore entry, and the fast-path read needs
            # last_writer_address.
            runbook_helpers.wait_nodes_caught_up(
                [topology.founder_grpc, topology.joiner_grpc],
                "sharedzone",
                api_key=api_key,
                timeout=30,
                probe_path=founder_vfs,
            )
            joiner_stat = runbook_helpers.vfs_stat(
                topology.joiner_grpc, founder_vfs, api_key=api_key, timeout=10
            )
            stat_result = joiner_stat.get("result") or {}
            assert stat_result.get("found"), (
                f"joiner metadata not replicated after raft catchup — stat={joiner_stat}"
            )
            last_writer = stat_result.get("lastWriterAddress") or ""
            assert "founder" in last_writer, (
                f"last_writer_address should point at founder, got {last_writer!r}. "
                "sys_write didn't capture self_address?"
            )

            # Step 4: joiner FUSE read — the whole chain end to end.
            # `cat /mnt/cc-tasks/founder/<rel>` flows: FUSE driver →
            # plugin event loop → sys_stat (metastore hit, replicated) →
            # sys_read → DT_MOUNT routes to LocalConnector → joiner's
            # backend says not-local → federation try_remote_fetch via
            # last_writer_address → founder's BlobFetcher → founder's
            # LocalConnector → founder's host fs.
            joiner_fuse_read = cc_tasks_share_helpers.mount_read_bytes(
                topology.joiner_container, founder_mount
            )
            assert joiner_fuse_read == founder_payload, (
                f"joiner FUSE cross-node read got {joiner_fuse_read!r}, "
                f"expected {founder_payload!r} — federation fan-out via "
                "FUSE broken"
            )

            # Step 5: symmetric — joiner writes via FUSE under joiner's
            # namespace; founder reads via FUSE.  Same chain reversed.
            rev_relpath = f"/{session}/rev.json"
            rev_payload = b'{"task":"cross-node-fuse","source":"joiner"}'
            joiner_vfs = topology.joiner_vfs_path(rev_relpath)
            joiner_mount = topology.mount_path_for_vfs(joiner_vfs)

            cc_tasks_share_helpers.mount_mkdir(
                topology.joiner_container,
                topology.mount_path_for_vfs(topology.joiner_vfs_path(f"/{session}")),
            )
            cc_tasks_share_helpers.mount_write_bytes(
                topology.joiner_container, joiner_mount, rev_payload
            )
            joiner_host_bytes = cc_tasks_share_helpers.host_task_read(
                topology.joiner_container, rev_relpath
            )
            assert joiner_host_bytes == rev_payload, (
                f"joiner FUSE write did not reach host fs: got {joiner_host_bytes!r}"
            )

            # Step 6: founder FUSE read via the reverse fan-out.  No
            # special-casing of founder as leader — the substrate must
            # work both ways identically.
            runbook_helpers.wait_nodes_caught_up(
                [topology.founder_grpc, topology.joiner_grpc],
                "sharedzone",
                api_key=api_key,
                timeout=30,
                probe_path=joiner_vfs,
            )
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


# ---------------------------------------------------------------------------
# Backend-only enumeration — the cc-tasks-list mega-goal shape.
#
# Claude Code writes task JSON via plain OS file operations
# (`fs.writeFileSync("~/.claude/tasks/<session>/<n>.json", ...)`) —
# nothing in Nexus's syscall surface ever runs.  No `vfs_write`, no
# metadata propose, no metastore entry.  The bytes live on the host
# fs the LocalConnector points at.
#
# For `cc tasks list` to enumerate those tasks through the FUSE mount,
# two pieces have to hold:
#   1. `sys_readdir` must merge the LocalConnector backend's
#      `list_dir(...)` output into its result set — this was already
#      in kernel/io.rs:3102-3118, but the DylibObjectStore wrapper
#      around dylib drivers returned `NotSupported` until
#      nexus-vfs#67 added the `nexus_driver_readdir` ABI symbol +
#      the LocalConnector implementation.
#   2. `sys_stat` must be symmetric — without a metastore entry it
#      historically returned `None` for backend-owned paths, so every
#      WinFsp `get_security_by_name` / FUSE `lookup` against the
#      enumerated entries failed with ENOENT.  nexus-vfs#67's
#      `sys_stat` backend.list_dir fallback closes that gap.
#
# This class pins both pieces by exercising the no-kernel-write path:
# write directly to `/host/tasks/<session>/<n>.json`, then enumerate +
# read via the FUSE mount.  A regression in either piece surfaces here.
# ---------------------------------------------------------------------------
class TestCcTasksListBackendOnlyEnumeration:
    """`cc tasks list` mega-goal pin — backend-only host-fs writes are
    discoverable + readable through the FUSE mount surface without any
    intervening kernel syscall write.

    Single-node first (the substrate); cross-node enumeration of
    backend-only entries is plan-deferred to a follow-up that lazily
    propagates listings across federation.
    """

    def test_host_fs_only_writes_enumerate_and_read_via_fuse(
        self,
        topology: CcTasksTopology,
        api_key: str,
        joined_cluster: dict,
    ) -> None:
        session = _new_session()
        # Drop three task JSONs straight onto founder's host fs — no
        # vfs_write, no metastore, no propose.  Mirrors what Claude
        # Code does at the OS layer outside Nexus.
        payloads = {
            "1.json": b'{"id":"1","status":"completed","backend_only":true}',
            "2.json": b'{"id":"2","status":"in_progress","backend_only":true}',
            "3.json": b'{"id":"3","status":"pending","backend_only":true}',
        }
        for name, data in payloads.items():
            cc_tasks_share_helpers.host_task_write(
                topology.founder_container, f"/{session}/{name}", data
            )

        try:
            # Step 1: backend-only writes do NOT create metastore
            # entries — the kernel-side surface knows nothing about
            # them until enumeration walks the backend.  Pin the
            # precondition so a future kernel change that quietly
            # starts propagating these wouldn't silently turn this
            # test into a different scenario.
            session_vfs = topology.founder_vfs_path(f"/{session}")
            pre_stat = runbook_helpers.vfs_stat(topology.founder_grpc, session_vfs, api_key=api_key)
            # `found` reflects the SSOT view: with PR #67 the kernel's
            # sys_stat backend-fallback DOES synthesise a DT_DIR
            # result for this path because the backend list_dir
            # returns the session dir among the mount's children.
            # That's exactly the contract this PR ships — the stat
            # surface is now symmetric with readdir over driver-owned
            # entries.  Keep the assertion narrow: we need `found`,
            # `is_directory`, AND `entry_type == 2 (DT_DIR)`.
            assert pre_stat["result"]["found"], (
                f"sys_stat must surface the backend-only session dir "
                f"after PR #67's fallback — got {pre_stat}"
            )
            assert pre_stat["result"]["isDirectory"], (
                f"backend-only session dir should stat as a directory — got {pre_stat}"
            )

            # Step 2: FUSE-level readdir of the session dir surfaces
            # every host-fs file we wrote.  This is the path
            # `cc tasks list` actually walks — the kernel readdir
            # delegates to the LocalConnector dylib's
            # nexus_driver_readdir symbol added in PR #67.
            session_mount = topology.mount_path_for_vfs(session_vfs)
            entries = cc_tasks_share_helpers.mount_listdir(
                topology.founder_container, session_mount
            )
            assert sorted(entries) == sorted(payloads.keys()), (
                f"FUSE readdir of backend-only session dir got {entries}, "
                f"expected {sorted(payloads.keys())} — driver_readdir wiring may be broken"
            )

            # Step 3: lookup each individual file via FUSE — exercises
            # the sys_stat backend-fallback path on a per-file basis,
            # the codepath that was returning ENOENT pre-PR.  Without
            # it, `cat M:\songym-win\<session>\1.json` failed because
            # WinFsp's get_security_by_name → sys_stat returned None.
            for name in payloads:
                file_vfs = topology.founder_vfs_path(f"/{session}/{name}")
                file_stat = runbook_helpers.vfs_stat(
                    topology.founder_grpc, file_vfs, api_key=api_key
                )
                assert file_stat["result"]["found"], (
                    f"sys_stat must find backend-only file {name} via "
                    f"backend.list_dir(parent) fallback — got {file_stat}"
                )
                assert not file_stat["result"]["isDirectory"], (
                    f"backend-only file {name} should stat as a regular file"
                )

            # Step 4: read the bytes back through the FUSE mount —
            # full chain `cat /mnt/.../<session>/<n>.json` → FUSE
            # lookup → sys_stat (backend fallback) → sys_open →
            # sys_read → LocalConnector backend → host fs.  Confirms
            # the enumeration entries map back to readable content.
            for name, expected in payloads.items():
                file_mount = topology.mount_path_for_vfs(
                    topology.founder_vfs_path(f"/{session}/{name}")
                )
                proc = subprocess.run(
                    ["docker", "exec", topology.founder_container, "cat", file_mount],
                    capture_output=True,
                    timeout=30,
                )
                assert proc.returncode == 0, (
                    f"FUSE read of {name} failed (rc={proc.returncode}): "
                    f"stderr={proc.stderr.decode(errors='replace')}"
                )
                assert proc.stdout == expected, (
                    f"FUSE read of {name} got {proc.stdout!r}, expected {expected!r}"
                )
        finally:
            runbook_helpers.docker_exec(
                topology.founder_container,
                ["rm", "-rf", f"/host/tasks/{session}"],
                check=False,
            )


# ---------------------------------------------------------------------------
# Cross-node backend-only enumeration — the `cc tasks list` Mac↔Win
# user-facing shape.
#
# Same `Claude Code writes JSON directly to ~/.claude/tasks/<session>/`
# scenario as TestCcTasksListBackendOnlyEnumeration above, but the
# enumerator is the OTHER node — the Mac↔Win operator workflow where
# host-fs writes on Mac must surface in Win's `cc tasks list` without
# any Nexus syscall ever firing on the writer side.
#
# Three things must hold for cross-node readdir of host-fs-only writes:
#
#   1. Same-zone DT_MOUNT rows replicate via raft so the joiner LEARNS
#      that `/shared/cc-tasks/<founder>` is a mount.  Already lands —
#      `dlc.mount`'s DT_MOUNT write goes to the parent zone's replicated
#      metastore.
#
#   2. The joiner's VFSRouter must install A routing entry for the
#      mount path.  Pre-PR, `wire_mount_core`'s same-zone branch
#      returned `Ok(())` without installing anything, so joiner's
#      `route()` on `/shared/cc-tasks/founder/...` fell through to
#      root and every sys_* returned not_found.  Closed by
#      nexus-vfs C3 — install a placeholder MountEntry with
#      backend=None + target_zone_id=Some when the local side has no
#      driver-mount.
#
#   3. With backend=None, the existing `backend.list_dir(...)` merge
#      in sys_readdir is skipped.  C4 added the FederationPeerClient
#      dispatch arm: route.backend.is_none() + target_zone_id.is_some()
#      → call `NexusVFSService.Readdir` on a non-self voter of the
#      target zone.  That voter — the SSOT side with the LocalConnector
#      backend — answers via its own backend.list_dir of the host fs.
#
# This class pins the new C3+C4 path by exercising it cross-node:
# write to founder's host fs, never touch any kernel syscall, then
# enumerate from the JOINER via FUSE readdir and assert the bytes
# round-trip back via gRPC Read (which uses the existing sys_read
# zone_peers fan-out — sys_stat dispatch through FederationPeerClient
# is the next gap and is intentionally NOT exercised here).
# ---------------------------------------------------------------------------
class TestCcTasksListBackendOnlyCrossNodeEnumeration:
    """Cross-node readdir of backend-only host-fs writes — Mac↔Win shape.

    Five-step workflow with strict data flow:

      (1) founder writes 3 task JSONs DIRECTLY to its host fs — no
          FUSE, no kernel syscall, no metastore propose.
      (2) sanity — the entries are visible via founder's FUSE readdir
          (the single-node substrate from PR #67).
      (3) joiner's FUSE readdir of the SAME federated dir surfaces
          the same entry NAMES via the new C3 placeholder MountEntry
          + C4 sys_readdir FederationPeerClient dispatch.  This is
          the new capability THIS PR introduces.
      (4) joiner's gRPC `NexusVFSService.Read` on each enumerated
          entry returns founder's bytes byte-exact — verifies the
          enumeration result isn't fabricated, the entries point at
          real readable content.
      (5) cleanup — `rm -rf /host/tasks/<session>` on founder.

    Data flow: (1)→(3) the entry NAMES the joiner enumerates ARE the
    files (1) wrote; (3)→(4) each name (3) returned is the input to
    (4)'s read.  Strong causal links — no step is "also called after".

    What this test does NOT yet cover (deferred follow-up):
      - Joiner FUSE `cat` of the enumerated entries.  FUSE `cat`
        flows through sys_stat first; sys_stat's FederationPeerClient
        dispatch is the next plan item (sister of this PR's sys_readdir
        dispatch).  Until that lands, joiner-side FUSE `cat` of a
        backend-only entry returns ENOENT — pinned in a separate
        xfail test below so the day sys_stat dispatch lands flips it
        green automatically.
      - `cc tasks list` end-to-end on joiner.  Same blocker — the CLI
        reads each session's metadata via sys_stat.
    """

    def test_founder_host_fs_writes_visible_via_joiner_fuse_readdir(
        self,
        topology: CcTasksTopology,
        api_key: str,
        joined_cluster: dict,
    ) -> None:
        session = _new_session()
        # Three host-fs-only payloads.  No FUSE write, no vfs_write —
        # bytes appear at /host/tasks/<session>/<name> on founder
        # ONLY.  Mirrors `fs.writeFileSync('~/.claude/tasks/...', ...)`
        # in Claude Code on Mac.
        payloads = {
            "1.json": b'{"id":"1","status":"completed","writer":"founder-host-fs"}',
            "2.json": b'{"id":"2","status":"in_progress","writer":"founder-host-fs"}',
            "3.json": b'{"id":"3","status":"pending","writer":"founder-host-fs"}',
        }
        for name, data in payloads.items():
            cc_tasks_share_helpers.host_task_write(
                topology.founder_container, f"/{session}/{name}", data
            )

        try:
            # Step 1: sanity — bytes are physically on founder's host
            # fs.  Establishes the workflow precondition; without it
            # the rest of the chain is meaningless.
            for name, expected in payloads.items():
                actual = cc_tasks_share_helpers.host_task_read(
                    topology.founder_container, f"/{session}/{name}"
                )
                assert actual == expected, (
                    f"founder host_task_write didn't land at /host/tasks/{session}/{name}: "
                    f"got {actual!r}, expected {expected!r}"
                )

            # Step 2: single-node substrate — founder's FUSE readdir
            # of the session dir shows every entry via PR #67's
            # backend.list_dir merge.  Same path the existing
            # TestCcTasksListBackendOnlyEnumeration pins — re-verified
            # here so a failure in step 3 (cross-node) can be cleanly
            # blamed on the cross-node arm, not the substrate.
            session_vfs = topology.founder_vfs_path(f"/{session}")
            session_mount = topology.mount_path_for_vfs(session_vfs)
            founder_entries = cc_tasks_share_helpers.mount_listdir(
                topology.founder_container, session_mount
            )
            assert sorted(founder_entries) == sorted(payloads.keys()), (
                f"founder single-node FUSE readdir broke — got {founder_entries}, "
                f"expected {sorted(payloads.keys())}.  PR #67 substrate regressed?"
            )

            # Step 3: THE C3+C4 CAPABILITY — joiner's FUSE readdir of
            # the SAME federated session dir surfaces founder's
            # host-fs-only entries.  Path:
            #   joiner FUSE ls
            #     → fuser dispatch
            #     → KernelHandle.sys_readdir(/shared/cc-tasks/founder/<sess>)
            #     → vfs_router.route(...) returns the placeholder
            #       MountEntry C3 installed (backend=None,
            #       target_zone_id=Some(sharedzone))
            #     → metastore merge: no entries (founder never wrote
            #       through any syscall — no metadata exists)
            #     → backend.list_dir branch SKIPPED (backend is None)
            #     → C4 federation-peer dispatch branch FIRES
            #     → FederationPeerClient.list_dir(peer=founder,
            #                                     path=<session>)
            #     → founder's NexusVFSService.Readdir handler
            #     → founder's kernel sys_readdir → its LocalConnector
            #       backend.list_dir → host fs
            #     → entries flow back to joiner, merged into seen
            #
            # An eventual-consistency window covers the placeholder
            # install (apply-cb fires after raft commit replicates).
            # 30 s budget matches the existing cross-node tests.
            deadline = time.monotonic() + 30
            joiner_entries: list[str] = []
            last_error: str | None = None
            while time.monotonic() < deadline:
                try:
                    joiner_entries = cc_tasks_share_helpers.mount_listdir(
                        topology.joiner_container, session_mount
                    )
                    if sorted(joiner_entries) == sorted(payloads.keys()):
                        break
                except Exception as exc:  # noqa: BLE001
                    last_error = repr(exc)
                time.sleep(0.5)
            assert sorted(joiner_entries) == sorted(payloads.keys()), (
                f"joiner cross-node FUSE readdir of backend-only entries failed — "
                f"got {joiner_entries}, expected {sorted(payloads.keys())} "
                f"(last_error={last_error}).  C3 placeholder MountEntry or C4 "
                f"sys_readdir FederationPeerClient dispatch broken?"
            )

            # Step 4: joiner FUSE cat of each enumerated entry returns
            # founder's bytes byte-exact.  This is the FULL user-facing
            # chain — FUSE driver → fuser lookup (sys_stat) → fuser
            # open / read (sys_read) → all cross-node via
            # FederationPeerClient dispatch.  The C5 sys_stat hook is
            # what makes FUSE lookup succeed for backend-only entries;
            # without it, `cat` would have returned ENOENT before
            # sys_read ever fired.
            for name, expected in payloads.items():
                file_mount = topology.mount_path_for_vfs(
                    topology.founder_vfs_path(f"/{session}/{name}")
                )
                joiner_bytes = cc_tasks_share_helpers.mount_read_bytes(
                    topology.joiner_container, file_mount
                )
                assert joiner_bytes == expected, (
                    f"joiner FUSE cat of {name} got {joiner_bytes!r}, "
                    f"expected {expected!r}. C5 sys_stat dispatch or "
                    f"sys_read zone_peers fan-out may have regressed."
                )

            # Step 5: sanity — direct gRPC Read also returns the same
            # bytes.  Decouples a FUSE-side regression from a kernel
            # syscall regression in failure messages.
            for name, expected in payloads.items():
                file_vfs = topology.founder_vfs_path(f"/{session}/{name}")
                read_resp = runbook_helpers.vfs_read(
                    topology.joiner_grpc,
                    file_vfs,
                    api_key=api_key,
                    timeout=30,
                )
                actual = (read_resp.get("result") or {}).get("content")
                assert actual == expected, (
                    f"joiner cross-node Read of {name} returned {actual!r}, "
                    f"expected {expected!r}. zone_peers fan-out / founder "
                    f"BlobFetcher may have regressed. full resp={read_resp}"
                )

        finally:
            runbook_helpers.docker_exec(
                topology.founder_container,
                ["rm", "-rf", f"/host/tasks/{session}"],
                check=False,
            )

    def test_joiner_fuse_cat_via_sys_stat_federation_peer_dispatch(
        self,
        topology: CcTasksTopology,
        api_key: str,
        joined_cluster: dict,
    ) -> None:
        """Focused pin on C5's sys_stat hook: FUSE cat of a backend-only entry.

        Smaller workflow than the multi-entry readdir + cat test above.
        Three steps:
          (1) write ONE host-fs-only file on founder;
          (2) joiner FUSE lookup (sys_stat over the federation
              boundary via C5);
          (3) joiner FUSE cat returns the same bytes.

        Pre-C5 this scenario failed at step (2) with ENOENT.  This
        test pins the regression — if any rework breaks sys_stat
        dispatch, this fails before any of the larger workflows do.
        """
        session = _new_session()
        payload = b'{"id":"x","writer":"founder-host-fs","via":"C5"}'
        cc_tasks_share_helpers.host_task_write(
            topology.founder_container, f"/{session}/x.json", payload
        )
        try:
            # Wait for joiner to see the entry via readdir (C3+C4)
            # before exercising stat (C5) — pre-C3 the placeholder
            # MountEntry doesn't exist yet, so stat would fall through
            # to root and fail for unrelated reasons.
            session_vfs = topology.founder_vfs_path(f"/{session}")
            session_mount = topology.mount_path_for_vfs(session_vfs)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if "x.json" in cc_tasks_share_helpers.mount_listdir(
                    topology.joiner_container, session_mount
                ):
                    break
                time.sleep(0.5)
            else:
                pytest.fail(
                    "joiner never saw x.json via readdir — C3+C4 substrate "
                    "broke; this test is conditional on readdir working."
                )

            # Step 1: joiner gRPC vfs_stat — direct surface for the
            # sys_stat call.  Pre-C5 returned `found=false`; post-C5
            # the federation-peer dispatch synthesises a StatResult
            # from founder's BackendStat.
            file_vfs = topology.founder_vfs_path(f"/{session}/x.json")
            stat_resp = runbook_helpers.vfs_stat(
                topology.joiner_grpc, file_vfs, api_key=api_key, timeout=15
            )
            assert stat_resp.get("result", {}).get("found"), (
                f"joiner vfs_stat must find backend-only x.json via C5 "
                f"sys_stat FederationPeerClient dispatch — got {stat_resp}"
            )
            assert not stat_resp.get("result", {}).get("isDirectory"), (
                f"backend-only x.json should stat as a regular file — got {stat_resp}"
            )

            # Step 2: joiner FUSE cat — full chain end to end.  FUSE
            # lookup goes through C5 sys_stat; FUSE read goes through
            # the existing zone_peers fan-out.
            file_mount = topology.mount_path_for_vfs(file_vfs)
            joiner_bytes = cc_tasks_share_helpers.mount_read_bytes(
                topology.joiner_container, file_mount
            )
            assert joiner_bytes == payload, (
                f"joiner FUSE cat got {joiner_bytes!r}, expected {payload!r}"
            )
        finally:
            runbook_helpers.docker_exec(
                topology.founder_container,
                ["rm", "-rf", f"/host/tasks/{session}"],
                check=False,
            )

    def test_cc_tasks_list_shape_end_to_end_cross_node(
        self,
        topology: CcTasksTopology,
        api_key: str,
        joined_cluster: dict,
    ) -> None:
        """`cc tasks list` end-to-end — the mega-goal of the whole effort.

        Mimics the actual user workflow:
          - Claude Code on Mac writes session JSONs directly to
            `~/.claude/tasks/<session>/<n>.json` (no Nexus syscall);
          - Operator on Win expects `cc tasks list` to enumerate the
            sessions + read their `1.json` metadata to surface task
            status.

        `cc tasks list` does roughly:
          1. readdir(`~/.claude/tasks/`) → session subdir names
          2. for each session: readdir(<session>/) → file names
          3. for each session: stat(<session>/1.json) → existence + size
          4. for each session: read(<session>/1.json) → metadata blob

        This test exercises all four ops cross-node (founder writes,
        joiner reads).  Strict data flow: step (2) consumes the names
        step (1) returned; step (3) consumes the size step (2) verified
        is non-empty; step (4) consumes the existence step (3)
        confirmed.  Every step has a meaningful assertion (not just
        type checks): entry counts, file sizes, byte-exact content.

        End-to-end success here is the proof that C3+C4+C5 together
        deliver the user-visible Mac↔Win `cc tasks list` capability.
        Failure isolates the broken layer via the more focused tests
        above.
        """
        # Two sessions with status metadata in `1.json` — the file
        # `cc tasks list` actually reads to render the session row.
        sessions = {
            _new_session(): b'{"id":"a","status":"completed","title":"Refactor X"}',
            _new_session(): b'{"id":"b","status":"in_progress","title":"Implement Y"}',
        }
        for sess, payload in sessions.items():
            cc_tasks_share_helpers.host_task_write(
                topology.founder_container, f"/{sess}/1.json", payload
            )

        try:
            # Operator-side `cc tasks list` walks
            # `~/.claude/tasks/<session>/`, which the runbook topology
            # maps onto `/shared/cc-tasks/founder/<session>/` mounted
            # at joiner's `/mnt/cc-tasks/founder/<session>/`.
            parent_vfs = topology.founder_vfs_path("")
            parent_mount = topology.mount_path_for_vfs(parent_vfs.rstrip("/"))

            # ── Step 1: enumerate sessions from joiner ──────────────
            # (parent readdir → session subdirs)
            deadline = time.monotonic() + 30
            seen_sessions: set[str] = set()
            while time.monotonic() < deadline:
                listing = cc_tasks_share_helpers.mount_listdir(
                    topology.joiner_container, parent_mount
                )
                seen_sessions = set(listing) & set(sessions.keys())
                if seen_sessions == set(sessions.keys()):
                    break
                time.sleep(0.5)
            assert seen_sessions == set(sessions.keys()), (
                f"`cc tasks list` parent enumeration: joiner saw "
                f"{sorted(seen_sessions)}, expected {sorted(sessions.keys())}. "
                "C3 placeholder MountEntry or C4 sys_readdir dispatch broke."
            )

            # ── Step 2: per session, list contents (the per-session
            # readdir `cc tasks list` does to find `1.json`) ─────────
            session_files: dict[str, list[str]] = {}
            for sess in sessions:
                sess_mount = topology.mount_path_for_vfs(topology.founder_vfs_path(f"/{sess}"))
                session_files[sess] = cc_tasks_share_helpers.mount_listdir(
                    topology.joiner_container, sess_mount
                )
                assert "1.json" in session_files[sess], (
                    f"session {sess} per-dir enumeration broke — got "
                    f"{session_files[sess]}, expected '1.json' to be present"
                )

            # ── Step 3: per session, stat `1.json` (the FUSE lookup
            # that gates the actual content read) ───────────────────
            for sess, expected_payload in sessions.items():
                file_vfs = topology.founder_vfs_path(f"/{sess}/1.json")
                stat_resp = runbook_helpers.vfs_stat(
                    topology.joiner_grpc, file_vfs, api_key=api_key, timeout=15
                )
                result = stat_resp.get("result", {})
                assert result.get("found"), (
                    f"`cc tasks list` stat for {sess}/1.json failed — got "
                    f"{stat_resp}. C5 sys_stat FederationPeerClient dispatch broke."
                )
                assert not result.get("isDirectory"), (
                    f"{sess}/1.json should stat as a file; got {stat_resp}"
                )
                # Size from peer's BackendStat survives the dispatch.
                reported_size = int(result.get("size", 0))
                assert reported_size == len(expected_payload), (
                    f"{sess}/1.json size mismatch — peer's BackendStat said "
                    f"{reported_size}, payload is {len(expected_payload)} bytes"
                )

            # ── Step 4: per session, read the JSON metadata (the
            # actual content `cc tasks list` parses) ────────────────
            for sess, expected_payload in sessions.items():
                file_mount = topology.mount_path_for_vfs(
                    topology.founder_vfs_path(f"/{sess}/1.json")
                )
                joiner_bytes = cc_tasks_share_helpers.mount_read_bytes(
                    topology.joiner_container, file_mount
                )
                assert joiner_bytes == expected_payload, (
                    f"`cc tasks list` content read for {sess}/1.json got "
                    f"{joiner_bytes!r}, expected {expected_payload!r}"
                )
        finally:
            for sess in sessions:
                runbook_helpers.docker_exec(
                    topology.founder_container,
                    ["rm", "-rf", f"/host/tasks/{sess}"],
                    check=False,
                )

    def test_joiner_fuse_unlink_propagates_to_founder_host_fs(
        self,
        topology: CcTasksTopology,
        api_key: str,
        joined_cluster: dict,
    ) -> None:
        """Operator-side `rm` from the federation peer reaches SSOT host fs.

        Three-step workflow:
          (1) founder writes a file directly to host fs (no Nexus syscall);
          (2) joiner FUSE `rm` propagates via C5's sys_unlink dispatch
              → founder's typed Delete handler → founder's sys_unlink
              → founder's LocalConnector delete_file → host fs row gone;
          (3) verify both founder's host fs AND joiner's FUSE see the
              entry as gone.

        Pins the systemic sys_unlink dispatch arm of C5 alongside the
        sys_readdir / sys_stat hooks.
        """
        session = _new_session()
        path_rel = f"/{session}/to-delete.json"
        cc_tasks_share_helpers.host_task_write(
            topology.founder_container, path_rel, b'{"will":"be deleted"}'
        )
        try:
            # Wait for joiner to see the entry via stat (C5) so we
            # know unlink will hit the dispatched arm, not the early
            # not-found path.
            file_vfs = topology.founder_vfs_path(path_rel)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                stat_resp = runbook_helpers.vfs_stat(
                    topology.joiner_grpc, file_vfs, api_key=api_key, timeout=10
                )
                if stat_resp.get("result", {}).get("found"):
                    break
                time.sleep(0.5)
            else:
                pytest.fail(
                    "joiner never saw the file via stat — C5 sys_stat "
                    "dispatch broke; unlink test prerequisite missing."
                )

            # Step 1: joiner FUSE unlink — full chain through C5
            # sys_unlink → FederationPeerClient.delete_file →
            # founder NexusVFSService.Delete → founder sys_unlink →
            # founder LocalConnector → host fs.
            file_mount = topology.mount_path_for_vfs(file_vfs)
            cc_tasks_share_helpers.mount_unlink(topology.joiner_container, file_mount)

            # Step 2: founder's host fs no longer has the file —
            # the SSOT side honoured the delete.
            host_full = f"/host/tasks{path_rel}"
            check = runbook_helpers.docker_exec(
                topology.founder_container,
                ["test", "-e", host_full],
                check=False,
            )
            assert check.rc != 0, (
                f"founder host fs still has {host_full} after joiner FUSE unlink — "
                f"C5 sys_unlink FederationPeerClient dispatch broke."
            )

            # Step 3: joiner's FUSE readdir no longer surfaces the entry —
            # the cross-node view caught up with the SSOT.
            session_mount = topology.mount_path_for_vfs(topology.founder_vfs_path(f"/{session}"))
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if "to-delete.json" not in cc_tasks_share_helpers.mount_listdir(
                    topology.joiner_container, session_mount
                ):
                    return
                time.sleep(0.5)
            pytest.fail(
                "joiner FUSE readdir still lists to-delete.json after unlink — "
                "post-delete enumeration cache stale or dispatch broke."
            )
        finally:
            runbook_helpers.docker_exec(
                topology.founder_container,
                ["rm", "-rf", f"/host/tasks/{session}"],
                check=False,
            )

    def test_joiner_fuse_write_lands_locally_and_founder_reads_via_peer_fetch(
        self,
        topology: CcTasksTopology,
        api_key: str,
        joined_cluster: dict,
    ) -> None:
        """Joiner FUSE write to peer-mount path stays local; founder reads via last-writer peer-fetch.

        Closes the write half of the cc-tasks-share cross-node loop under
        the **uniform local-first sys_write contract** (nexus-vfs PR #98).
        sys_readdir (PR #4427) lets the joiner SEE peer-owned files,
        sys_unlink (PR #4427) lets the joiner REMOVE them, this pin
        covers the joiner CREATING/UPDATING them.

        End-to-end workflow under the new contract:
          (1) joiner FUSE `cat > <mount-path>` runs through the plugin's
              KernelHandle.write → kernel sys_write → with placeholder
              MountEntry shape (backend=None + target_zone_id=Some)
              the kernel substitutes its own federation-cache backend
              (`<data_dir>/federation-cache/<canonical-path>`) — bytes
              stay on JOINER's host fs, NOT founder's.  metastore.put
              stamps `last_writer_address = joiner_self`; raft
              replicates the entry to founder.
          (2) joiner FUSE re-reads its just-written file: writer-side
              fast path — sys_read sees `last_writer == self`, serves
              bytes from federation-cache.  Round-trip closed locally
              with NO Tailscale hop.
          (3) founder's host fs at /host/tasks/<sess>/new.json stays
              EMPTY — the bytes never crossed.  This is the byte-
              residence semantic that distinguishes the new contract
              from PR #80's defer-to-peer (where bytes would have
              landed on founder host fs).
          (4) founder FUSE cat of the same path returns joiner's
              bytes byte-exact — sys_read backend-miss → metastore
              entry says `last_writer = joiner` → try_remote_fetch
              dispatches peer_read(joiner_addr, path) → joiner's gRPC
              sys_read handler hits the federation-cache substitution
              and serves bytes back.

        Pins:
          * The kernel-global federation_cache substitution in
            io.rs::sys_write (both arms: route.backend is None +
            target_zone is Some → substitute cache backend).
          * The writer-side fast path in io.rs::sys_read
            (last_writer == self → federation_cache.read_content).
          * The last-writer-aware peer-fetch fallback in
            io.rs::try_remote_fetch (last_writer != self →
            peer_read via DistributedCoordinator).
        """
        session = _new_session()
        path_rel = f"/{session}/new.json"
        # A non-trivial payload so a byte-stream-truncation regression
        # would show up as a length mismatch, not just a content one.
        payload = b'{"id":"c","status":"new","title":"Drafted via cross-node FUSE write"}'
        try:
            # Seed the session directory on founder host fs first — the
            # realistic shape the operator workflow has (Mac CC creates
            # the session dir, then Win operator writes new files into
            # it via the FUSE mount).  Without an existing parent dir,
            # the FUSE create-write call fails with "Directory
            # nonexistent" because neither the kernel nor the plugin
            # implicitly mkdir's parent paths.  Wait until the joiner
            # side sees the seeded dir via readdir so we know raft has
            # replicated the parent before issuing the FUSE write.
            cc_tasks_share_helpers.host_task_ensure_dir(topology.founder_container, f"/{session}")
            parent_vfs = topology.founder_vfs_path("")
            parent_mount = topology.mount_path_for_vfs(parent_vfs.rstrip("/"))
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if session in cc_tasks_share_helpers.mount_listdir(
                    topology.joiner_container, parent_mount
                ):
                    break
                time.sleep(0.5)
            else:
                pytest.fail(
                    f"joiner never saw seeded session dir {session} via readdir "
                    "— sys_readdir federation peer dispatch broke or raft drain hung; "
                    "sys_write test prerequisite missing."
                )

            # Step 1: joiner FUSE create + write — under the new
            # uniform local-first contract, this lands bytes on
            # JOINER's federation_cache at
            # `<joiner_data_dir>/federation-cache/<canonical-path>`,
            # NOT on founder host fs.  metastore.put stamps
            # `last_writer = joiner` and raft replicates.
            file_vfs = topology.founder_vfs_path(path_rel)
            file_mount = topology.mount_path_for_vfs(file_vfs)
            cc_tasks_share_helpers.mount_write_bytes(topology.joiner_container, file_mount, payload)

            # Step 2: joiner FUSE round-trip read — writer-side fast
            # path is synchronous (sys_read on the placeholder mount
            # sees `last_writer == self` and serves bytes from
            # federation_cache directly; no peer hop, no raft-apply
            # wait).  `mount_write_bytes` above returns only after
            # sys_write's federation_cache write + metastore.put both
            # commit on joiner — by the time we get here, the entry
            # IS in joiner's local metastore + federation_cache.
            # Single read, no polling.
            joiner_bytes = cc_tasks_share_helpers.mount_read_bytes(
                topology.joiner_container, file_mount
            )
            assert joiner_bytes == payload, (
                f"joiner FUSE read got {joiner_bytes!r} for the file it "
                f"just wrote, expected {payload!r}. "
                "Writer-side fast path broken — io.rs::sys_read "
                "last_writer==self federation_cache substitution didn't fire."
            )

            # Step 3: founder host fs at /host/tasks/<sess>/new.json
            # stays EMPTY — the bytes never crossed under the new
            # contract.  This is the byte-residence semantic that
            # distinguishes the new local-first contract from PR #80's
            # defer-to-peer (where bytes WOULD have landed on
            # founder host fs).  Bounded read attempt; existence is
            # the failure signal.
            host_full = f"/host/tasks{path_rel}"
            host_present = runbook_helpers.docker_exec(
                topology.founder_container,
                ["test", "-e", host_full],
                check=False,
            )
            assert host_present.rc != 0, (
                f"founder host fs at {host_full} unexpectedly received the "
                "joiner-side FUSE write — the uniform local-first sys_write "
                "contract (nexus-vfs PR #98) is regressed; bytes should "
                "stay on the joiner's federation_cache."
            )

            # Step 4: founder FUSE cat returns joiner's bytes byte-
            # exact via the last-writer-aware cross-peer fetch chain.
            #
            # The wait here is for raft replication of joiner's
            # metastore.put to land on founder's sharedzone state
            # machine — a known async latency window.  Decompose into:
            #
            #   (a) event-driven wait on founder's gRPC sys_stat
            #       returning `found=true` for the file path (binary
            #       signal: either replicated or not — no byte
            #       comparison ambiguity);
            #   (b) single deterministic cat once (a) succeeds —
            #       sys_read goes backend-miss → try_remote_fetch →
            #       joiner's KernelBlobFetcher::read consults the
            #       kernel-global federation cache (nexus-vfs#99
            #       SSOT-symmetric fix) and serves bytes back; founder
            #       caches them locally + returns them to FUSE.
            #
            # Replaces the previous "cat in a polling loop with
            # byte-equality match" pattern which conflated three
            # failure signals (replication latency, transient grpc
            # errors, partial-byte reads) into one timeout.
            file_zone = "sharedzone"
            deadline = time.monotonic() + 30
            stat_found = False
            last_stat_err = ""
            while time.monotonic() < deadline:
                stat_resp = runbook_helpers.vfs_stat(
                    topology.founder_grpc, file_vfs, zone_id=file_zone, timeout=5
                )
                stat_inner = stat_resp.get("result", {})
                if stat_inner.get("found"):
                    stat_found = True
                    break
                last_stat_err = repr(stat_resp.get("error") or stat_inner)
                time.sleep(0.5)
            assert stat_found, (
                f"founder gRPC sys_stat({file_vfs}) never returned found=true "
                f"within 30s — raft replication of joiner's metastore.put did "
                f"not land on founder (last_resp={last_stat_err})."
            )

            # (b) Now read the bytes — single deterministic call.
            founder_bytes = cc_tasks_share_helpers.mount_read_bytes(
                topology.founder_container, file_mount
            )
            assert founder_bytes == payload, (
                f"founder FUSE cat got {founder_bytes!r}, expected "
                f"{payload!r}.  Cross-peer fetch broken — nexus-vfs#99's "
                "KernelBlobFetcher::read federation_cache substitution "
                "regressed, OR try_remote_fetch's peer_read RPC fails "
                "to reach the writer."
            )
        finally:
            runbook_helpers.docker_exec(
                topology.founder_container,
                ["rm", "-rf", f"/host/tasks/{session}"],
                check=False,
            )
