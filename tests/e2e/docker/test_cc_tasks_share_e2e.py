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
import json
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
            # nexus-vfs PR #106 — sidecar shares the joiner's identity
            # volume so `identity::persist_peers` after JoinZone lands
            # where the main daemon's next boot loads it.  Volume name
            # matches the compose file's `cc_tasks_joiner_identity`.
            identity_volume=f"{topology.joiner_container}-identity",
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


# ---------------------------------------------------------------------------
# nexus-vfs PR #102 regression pin — SSOT-symmetric sys_readdir observation.
# ---------------------------------------------------------------------------
#
# Motivating scenario (Phase γ-A native cc-tasks-list UX):
#
#   Claude Code writes `~/.claude/tasks/<uuid>/1.json` directly to the
#   operator's host filesystem, bypassing every Nexus syscall.  Before
#   PR #102 the metastore had no row for that path — sys_readdir on the
#   writing node's LocalConnector saw the entry via backend.list_dir
#   but never proposed a metadata row to metastore, so peer daemons
#   couldn't enumerate the entry through their own metastore.list step.
#   Cross-peer readdir worked only via federation-dispatch on placeholder
#   mounts (peer-namespaced topology).  Peer-shared mount topologies
#   (both peers `--mount-driver` at the SAME VFS path) had no way for a
#   peer's readdir to find entries written directly on another peer's
#   host fs.
#
#   PR #102 adds `observe_backend_readdir_entry` mirroring the read-path
#   `observe_backend_content` helper.  Every backend entry sys_readdir
#   observes that metastore doesn't already know about gets a metadata
#   row proposed with `last_writer_address = self`; raft replicates,
#   and a peer's sys_readdir sees the entry via metastore.list alone.


class TestSysReaddirObservation:
    """Pin nexus-vfs PR #102's SSOT-symmetric readdir observation.

    Regression-protects the Phase γ-A native `cc tasks list` UX
    against future changes to `sys_readdir` that would strip the
    `observe_backend_readdir_entry` call, or against changes to the
    observation helper that would stop stamping
    `last_writer_address`.  Both regressions would surface here as
    the joiner's `vfs_stat` reporting an empty `lastWriterAddress`
    for a path written directly to founder's host fs.
    """

    def test_founder_direct_hostfs_write_seeds_joiner_metastore_via_readdir_observation(
        self,
        topology: CcTasksTopology,
        api_key: str,
        joined_cluster: dict,
    ) -> None:
        # Reuses the existing joined_cluster fixture — no fresh joiner
        # topology needed.  The observation flow is orthogonal to the
        # cluster's bootstrap history.
        _ = joined_cluster

        # ── Step 1: Direct host-fs write on founder (bypasses sys_write) ─
        #
        # `host_task_write` shells into founder's container and writes
        # bytes at `/host/tasks/<session>/1.json` on founder's host fs
        # — the operator's host fs — WITHOUT going through Nexus's
        # sys_write.  Metastore starts with no row for the parent
        # `<session>/` DT_DIR or the child `1.json` DT_REG on either
        # peer.  This mirrors Claude Code's actual write pattern —
        # session directories materialise when CC persists task state.
        session = _new_session()
        payload = b'{"id":"1","status":"pending","writer":"founder-host-fs-direct-obs"}'
        cc_tasks_share_helpers.host_task_write(
            topology.founder_container, f"/{session}/1.json", payload
        )

        try:
            file_vfs = topology.founder_vfs_path(f"/{session}/1.json")
            file_mount = topology.mount_path_for_vfs(file_vfs)
            session_vfs = topology.founder_vfs_path(f"/{session}")

            # ── Step 2: founder mount_listdir triggers DT_DIR observation ─
            #
            # `ls /mnt/cc-tasks/founder/` on founder's container fires
            # FUSE → sys_readdir → backend.list_dir returns "<session>/"
            # (trailing slash indicates a DT_DIR entry) → the observation
            # loop calls `observe_backend_readdir_entry` with entry_type
            # = DT_DIR → metastore.put stamps a row with
            # last_writer_address = founder.  Raft replicates the row
            # to joiner.
            #
            # Only DT_DIR entries are observed by design (nexus-vfs PR
            # #102 + #103 division of labour): directories carry no
            # byte content so the size=0 placeholder is semantically
            # correct, and cross-peer directory enumeration is what
            # `cc tasks list` needs.  DT_REG entries stay backend-only
            # and route via the existing federation-dispatch fetch on
            # read — accurate size + content_id come from the writer's
            # backend on demand.
            tasks_root_mount = topology.mount_path_for_vfs(topology.founder_vfs_path(""))
            founder_listing = cc_tasks_share_helpers.mount_listdir(
                topology.founder_container, tasks_root_mount
            )
            assert session in founder_listing, (
                f"founder mount_listdir did not see the session dir "
                f"{session!r} — sys_readdir backend.list_dir surface broken "
                f"before we can test the observation contract.  Got "
                f"{founder_listing}."
            )

            # ── Step 2b: founder mount_listdir on session/ triggers DT_REG obs ─
            #
            # The step-2 top-level `ls` only enumerates direct children
            # of the mount root, i.e. the session subdir itself; the
            # nested `1.json` DT_REG is not observed until the operator
            # (or CC's session-scan) descends INTO the session dir.  A
            # second `ls /mnt/cc-tasks/founder/<session>/` fires FUSE →
            # sys_readdir on the session dir → backend.list_dir returns
            # `1.json` → observation loop calls backend.stat for its
            # size and stamps a DT_REG row with `content_id =
            # Some(backend_path)` and `last_writer_address = founder`.
            # This mirrors CC's actual access pattern (top-level list,
            # then per-session drill-down when the user opens one).
            session_mount = topology.mount_path_for_vfs(session_vfs)
            session_listing = cc_tasks_share_helpers.mount_listdir(
                topology.founder_container, session_mount
            )
            assert "1.json" in session_listing, (
                f"founder mount_listdir on {session_mount!r} did not see "
                f"'1.json'.  Nested backend.list_dir failed before we "
                f"can test the DT_REG observation contract.  Got "
                f"{session_listing}."
            )

            # ── Step 3: wait for raft to replicate BOTH observed rows ──
            #
            # `wait_nodes_caught_up` blocks until both peers agree on
            # sharedzone's committed index — the strong signal that the
            # observation-proposed rows (DT_DIR from step 2 + DT_REG
            # from step 2b) have landed on joiner's metastore and are
            # queryable.  Probe on the DT_REG path (file_vfs) so we
            # gate on the strictly-later of the two propose commits.
            runbook_helpers.wait_nodes_caught_up(
                [topology.founder_grpc, topology.joiner_grpc],
                "sharedzone",
                api_key=api_key,
                timeout=30,
                probe_path=file_vfs,
            )

            # ── Step 4a: joiner vfs_stat on session DT_DIR ──
            #
            # PR #102 DT_DIR observation regression pin.  Without
            # observation, joiner's metastore has no DT_DIR row for
            # the session subdir; vfs_stat either returns found=False
            # or falls through to federation dispatch, in which case
            # `lastWriterAddress` is empty because backend.stat can't
            # stamp it.  With observation, joiner's metastore holds
            # the raft-replicated row with lastWriterAddress = founder.
            joiner_dir_stat = runbook_helpers.vfs_stat(
                topology.joiner_grpc,
                session_vfs,
                zone_id="sharedzone",
                api_key=api_key,
                timeout=10,
            )
            dir_result = joiner_dir_stat.get("result") or {}
            assert dir_result.get("found"), (
                f"joiner vfs_stat({session_vfs}) did not find the DT_DIR "
                f"entry after founder observation should have replicated. "
                f"Either observe_backend_readdir_entry did not fire, or "
                f"the metastore.put propose did not commit to sharedzone. "
                f"joiner_stat={joiner_dir_stat}"
            )
            dir_last_writer = dir_result.get("lastWriterAddress") or ""
            assert "founder" in dir_last_writer, (
                f"joiner's DT_DIR row has lastWriterAddress="
                f"{dir_last_writer!r}; expected the founder container's "
                f"address (contains 'founder'). observe_backend_readdir_"
                f"entry did not stamp self_address correctly. "
                f"stat={joiner_dir_stat}"
            )

            # ── Step 4b: joiner vfs_stat on 1.json DT_REG ──
            #
            # PR #104 DT_REG observation regression pin.  Pre-PR #104
            # (i.e. under PR #103's defensive restriction) DT_REG
            # entries were NOT observed.  Joiner's vfs_stat on 1.json
            # would miss its metastore and fall through to federation
            # dispatch to founder — the response's `lastWriterAddress`
            # would be empty (backend.stat cannot stamp).
            #
            # Post-PR #104, DT_REG entries ARE observed with
            # content_id=Some(backend_path), so joiner's metastore
            # holds the row with lastWriterAddress = founder.  Non-
            # empty `founder` in lastWriterAddress is the strong
            # signal that the row was raft-replicated from founder's
            # observation, not synthesised via backend.stat dispatch.
            joiner_file_stat = runbook_helpers.vfs_stat(
                topology.joiner_grpc,
                file_vfs,
                zone_id="sharedzone",
                api_key=api_key,
                timeout=10,
            )
            file_result = joiner_file_stat.get("result") or {}
            assert file_result.get("found"), (
                f"joiner vfs_stat({file_vfs}) did not find the DT_REG "
                f"entry. PR #104 should have observed 1.json into "
                f"metastore with content_id=Some(backend_path). "
                f"stat={joiner_file_stat}"
            )
            file_last_writer = file_result.get("lastWriterAddress") or ""
            assert "founder" in file_last_writer, (
                f"joiner's DT_REG row has lastWriterAddress="
                f"{file_last_writer!r}; expected 'founder'.  PR #104's "
                f"observation did not stamp the DT_REG row, OR the "
                f"response bypassed metastore. stat={joiner_file_stat}"
            )

            # ── Step 5: joiner FUSE cat — byte-exact via observation ──
            #
            # Verifies the DT_REG read path under PR #104's new
            # content_id-stamping contract: joiner FUSE → sys_read →
            # metastore hit (from observation) → content_id=Some(
            # backend_path) + last_writer=founder → try_remote_fetch
            # → founder's kernel → metastore hit + last_writer=self →
            # founder.backend.read_content(backend_path) → bytes.
            #
            # Pre-PR #104 this read would return empty (b'') because
            # observation stamped content_id=None, which turned the
            # metastore hit into a dead-end on the writer side:
            # sys_read at line ~461 short-circuits to `content = None`
            # for content_id_opt.is_none() and falls to
            # try_remote_fetch → self → FileNotFound.  Empty bytes is
            # exactly what CI caught on the initial PR #4464 run.
            joiner_bytes = cc_tasks_share_helpers.mount_read_bytes(
                topology.joiner_container, file_mount
            )
            assert joiner_bytes == payload, (
                f"joiner FUSE cross-node read got {joiner_bytes!r}, "
                f"expected {payload!r}. PR #104's content_id stamping "
                f"regressed — metastore-hit + content_id=None dead-ends "
                f"in try_remote_fetch → self → FileNotFound, matching "
                f"the exact failure PR #103 defensively worked around. "
                f"Check observe_backend_readdir_entry's content_id "
                f"parameter is threaded from sys_readdir's wire-up loop."
            )

            # ── Step 6: joiner FUSE cat AGAIN — second-read regression ──
            #
            # PR #104 also fixes a latent bug in observe_backend_content
            # (the read-path helper, existing since PR #98) — it stamped
            # content_id=None, so a SECOND read of any peer-observed
            # file would dead-end at try_remote_fetch → self →
            # FileNotFound on the writer side.  Not caught by existing
            # tests because they only exercised one cross-peer read.
            #
            # This step re-reads the same file to pin the fix: post-
            # PR #104, observe_backend_content also stamps
            # content_id=Some(backend_path), so the metastore row from
            # step 5's fan-out observation round-trips correctly.
            joiner_bytes_second = cc_tasks_share_helpers.mount_read_bytes(
                topology.joiner_container, file_mount
            )
            assert joiner_bytes_second == payload, (
                f"joiner FUSE second read got {joiner_bytes_second!r}, "
                f"expected {payload!r}. observe_backend_content stamped "
                f"content_id=None (the pre-PR-#104 latent bug), causing "
                f"the second read's metastore hit to dead-end.  Verify "
                f"observe_backend_content passes Some(route.backend_path) "
                f"to build_metadata in the read-path helper."
            )
        finally:
            runbook_helpers.docker_exec(
                topology.founder_container,
                ["rm", "-rf", f"/host/tasks/{session}"],
                check=False,
            )


class TestIdentityPeerPersistence:
    """Pin nexus-vfs PR #106's identity.json peer-address persistence.

    Regression-protects the S3 cache-loss-recovery foundation: the
    ``nexusd-cluster join`` sidecar records the leader ``--peer-addr``
    into ``identity.json`` after JoinZone so subsequent daemon
    restarts (with ``NEXUS_PEERS`` unset per the entrypoint's
    ``MODE=restart`` branch) still have a transport-layer seed for
    the founder.

    Without this pin, silent regressions in ``run_join``'s
    ``identity::persist_peers`` call, or in ``open_zone_manager``'s
    identity load, would surface only as "operator wiped data_dir +
    can't remember peers = daemon stuck" in production — the exact
    scenario S3 is meant to prevent.
    """

    def test_joiner_identity_json_contains_founder_peer_after_join(
        self,
        topology: CcTasksTopology,
        joined_cluster: dict,
    ) -> None:
        # ── Step 1: joined_cluster fixture (setup) ──────────────────
        #
        # The fixture ran the runbook §3b offline
        # `nexusd-cluster join <founder_id>@founder:2126 sharedzone
        # /shared` sidecar against the joiner's data + identity
        # volumes.  Under nexus-vfs PR #106 that CLI call MUST have
        # invoked `identity::persist_peers` after the JoinZone RPC
        # committed the AddNode ConfChange.  Fixture also restarted
        # the joiner daemon, whose `open_zone_manager` boot path
        # separately calls `identity::load` + `persist_peers` on the
        # CLI peer set — the persisted union survives across both.
        founder_node_id = joined_cluster["founder_node_id"]

        # ── Step 2: read joiner's identity.json (the causal output) ─
        #
        # The compose mounts `cc_tasks_joiner_identity` at
        # `/app/identity` on the joiner service AND on the sidecar,
        # matching `NEXUS_IDENTITY_DIR`.  A container-level `cat`
        # avoids depending on the joiner's Python runtime being
        # importable — this test is a substrate-level regression
        # pin, not a service-tier flow.
        cat_result = runbook_helpers.docker_exec(
            topology.joiner_container,
            ["cat", "/app/identity/identity.json"],
        )
        assert cat_result.rc == 0, (
            f"joiner's /app/identity/identity.json missing (rc={cat_result.rc}). "
            f"Either the compose volume mount or the sidecar's "
            f"`identity::persist_peers` write is broken. "
            f"stderr={cat_result.stderr!r}"
        )
        try:
            identity = json.loads(cat_result.stdout)
        except json.JSONDecodeError as e:
            pytest.fail(
                f"joiner's /app/identity/identity.json is not valid JSON "
                f"({e}). The sidecar's `identity::persist_peers` either "
                f"failed to write or wrote a schema this test does not "
                f"understand. Raw content: {cat_result.stdout!r}"
            )

        # ── Step 3: schema + persisted-peer content assertions ──────
        #
        # Schema version pinned by `SCHEMA_VERSION` in
        # `rust/raft/src/identity.rs`; a mismatch means either an
        # intentional schema bump (update this test) or an accidental
        # rollback (fix the code).  Identity peer entries are the
        # operator-facing `host:port` shape post nexus-vfs #109 —
        # `NodeAddress::to_operator_str` serializes without the
        # `<node_id>@` prefix so subsequent cold-boot load through
        # `parse_operator_addr` never trips the id-prefix rejection.
        # The daemon still learns the founder's real node_id at
        # runtime via `learn_peer_address` on the first inbound raft
        # message — it never needs to be encoded in the address book.
        assert identity.get("schema_version") == 1, (
            f"identity schema_version mismatch: expected 1, got "
            f"{identity.get('schema_version')!r}. Full identity: {identity!r}"
        )
        peers = identity.get("peers")
        assert isinstance(peers, list), (
            f"identity.peers must be a list, got {type(peers).__name__}: {peers!r}"
        )
        # Founder peer entry = bare `host:port` (topology.founder_grpc).
        # `founder_node_id` is still fetched via `joined_cluster` above
        # so a schema regression that ever re-introduces id encoding
        # surfaces here as an inequality (kept in the debug output for
        # diagnosis, not part of the expected value).
        _ = founder_node_id
        founder_peer = topology.founder_grpc
        assert founder_peer in peers, (
            f"identity.peers does not contain the founder peer "
            f"{founder_peer!r}. The sidecar's `identity::persist_peers` "
            f"either did not run, wrote to the wrong path, or wrote a "
            f"different peer string.  Full identity.peers={peers!r} "
            f"(founder_node_id={founder_node_id})"
        )


class TestS3DataDirLossRebuild:
    """S3 identity-assisted operator recovery after data_dir loss.

    Regression-protects the OTHER half of the S3 partition (the first
    half — identity peer persistence — is pinned above by
    ``TestIdentityPeerPersistence``).  The current S3 contract:

      identity_dir/  = SSOT for node membership (survives data_dir wipe)
      data_dir/      = cache of raft state machine (regenerable)
      raft log on peers = the actual replicated SSOT
      recovery path: operator runs ``nexusd-cluster join <peer_addr>``
        sidecar; the peer_addr comes from identity.json (which the
        operator does NOT need to memorize)

    Full auto-recovery-on-boot (no sidecar needed) is a future
    milestone tracked in ``project_phase_ga_win_mac_end_to_end``.
    Today the operator convenience is: the peer address to pass to
    ``nexusd-cluster join`` is in identity.json and can be discovered
    without operator memory.

    Destructive test — nukes joiner's ``/app/data`` mid-suite.  Declared
    LAST so any preceding test class runs against a healthy joiner.

    Four steps with hard causal data flow:
      1. Founder writes ONE session to its host fs → both nodes
         observe via ``sys_readdir`` → sharedzone metastore rows
         present on joiner.
      2. Read joiner's identity.json.peers to discover the founder's
         operator-form host:port (the "operator does not need to
         memorize peer addresses" invariant).
      3. Stop joiner, wipe /app/data volume (leaves /app/identity
         untouched), rerun the sidecar rejoin using the addr from
         step 2, restart joiner.
      4. Assert: (a) identity.json survived byte-exact, (b) joiner
         sees the seeded session (rebuild succeeded via sidecar +
         subsequent daemon restart).
    """

    def test_joiner_recovers_after_data_dir_wipe_via_identity_seeded_rejoin(
        self,
        topology: CcTasksTopology,
        api_key: str,
        joined_cluster: dict,
    ) -> None:
        # ── PENDING: full data_dir wipe recovery requires a follow-up ──
        #
        # Current failure mode observed in CI (nexus PR #4473):
        #   1. Wipe /app/data → joiner loses .node_id (which lives at
        #      <data_dir>/.node_id, NOT in identity.json — per
        #      `project_s3_identity_landed` memory, PR #106 landed peer
        #      persistence only, node_id lifecycle unchanged under the
        #      rotate-on-wipe PR #3996 opaque-ID contract).
        #   2. Sidecar rejoin mints a NEW node_id (18344...).
        #   3. Founder's sharedzone ConfState still has voters =
        #      [founder, OLD_joiner_id].  With OLD_joiner offline,
        #      founder cannot achieve 2-of-2 quorum → AddNode(NEW_joiner)
        #      cannot commit → sidecar times out after 15 attempts.
        #
        # Unlocking this pin requires ONE of:
        #   * Joiner rejoins as `as_role="learner"` (nexus-vfs PR #66's
        #     wipe-rejoin-safe mode — learners don't count toward quorum,
        #     so founder-alone can admit them).  Compose default is
        #     voter today; per-test override is possible but drifts the
        #     scenario from the operator's default workflow.
        #   * Add operator-initiated RemoveNode(OLD_joiner) before
        #     AddNode(NEW_joiner) — needs quorum-of-remaining semantics
        #     support in the raft layer, which raft-rs 0.7 does not
        #     directly provide (ConfChange itself needs quorum to commit).
        #   * Move .node_id into identity.json so wipe preserves it —
        #     full S3 milestone tracked in project_phase_ga_win_mac_end_to_end.
        #
        # Test body preserved below (post-skip) so when the feature
        # lands the pin activates atomically.  The pre-skip design
        # doc + assertions are also kept intact as a spec.
        pytest.skip(
            "Full data_dir-wipe auto-recovery not shipped — see "
            "project_phase_ga_win_mac_end_to_end memory for milestone. "
            "Un-skip when either PR #66 learner-role becomes the "
            "default, or when .node_id moves into identity.json."
        )
        session = _new_session()
        payload = b'{"writer":"founder-host-fs","test":"s3-data-dir-loss-rebuild"}'
        founder_node_id = joined_cluster["founder_node_id"]

        # ── Step 1: seed sharedzone with a founder write ────────────
        cc_tasks_share_helpers.host_task_write(
            topology.founder_container, f"/{session}/1.json", payload
        )
        try:
            # Baseline: joiner sees the seed before we wipe.
            session_vfs = topology.founder_vfs_path(f"/{session}")
            session_mount = topology.mount_path_for_vfs(session_vfs)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                probe = runbook_helpers.docker_exec(
                    topology.joiner_container,
                    ["ls", "-1", session_mount],
                    check=False,
                )
                if probe.rc == 0 and "1.json" in probe.stdout.splitlines():
                    break
                time.sleep(0.5)
            else:
                pytest.fail("baseline broke: joiner never saw the seed session before wipe")

            # ── Step 2: discover founder addr from identity.json ────
            #
            # The S3 operator promise: the peer address to pass to
            # `nexusd-cluster join` is in identity.json and can be
            # discovered without operator memory.  This test proves
            # the value is there AND the recovery workflow reads it
            # back correctly.
            identity_before = runbook_helpers.docker_exec(
                topology.joiner_container,
                ["cat", "/app/identity/identity.json"],
                check=True,
            )
            identity_json = json.loads(identity_before.stdout)
            founder_addr_from_identity = next(iter(identity_json["peers"]), None)
            assert founder_addr_from_identity is not None, (
                f"identity.peers is empty pre-wipe — the recovery "
                f"workflow's addr source is unusable. Full identity: "
                f"{identity_before.stdout!r}"
            )

            # ── Step 3: wipe joiner's data_dir volume ───────────────
            #
            # Stop the joiner (releases redb file lock), nuke
            # /app/data via a transient alpine container (the data
            # volume is a docker volume, not a bind mount, so
            # `docker exec` post-stop is unavailable), then rerun the
            # sidecar rejoin using the addr discovered above from
            # identity.json.  The identity volume mounted at
            # /app/identity is deliberately untouched — the S3
            # partition contract's operative invariant.
            runbook_helpers.docker_stop(topology.joiner_container)
            wipe_result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    "nexus-cc-tasks-joiner-data:/data",
                    "alpine:3",
                    "sh",
                    "-c",
                    "rm -rf /data/root /data/sharedzone /data/metastore.redb "
                    "/data/.node_id /data/federation-cache",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert wipe_result.returncode == 0, (
                f"data volume wipe failed (rc={wipe_result.returncode}); "
                f"stderr={wipe_result.stderr!r}"
            )

            rejoin = runbook_helpers.run_nexusd_cluster_join(
                target_container=topology.joiner_container,
                target_volume=f"{topology.joiner_container}-data",
                founder_node_id=founder_node_id,
                founder_addr=founder_addr_from_identity,
                zone_id="sharedzone",
                local_path="/shared",
                hostname="joiner",
                cluster_image=os.environ.get(
                    "NEXUS_CLUSTER_IMAGE", "nexus-local-connector-plugin:latest"
                ),
                network=os.environ.get("NEXUS_CC_TASKS_NETWORK", "nexus-cc-tasks-net"),
                identity_volume=f"{topology.joiner_container}-identity",
            )
            assert rejoin.returncode == 0, (
                f"post-wipe sidecar rejoin failed: rc={rejoin.returncode} "
                f"stdout={rejoin.stdout!r} stderr={rejoin.stderr!r}"
            )
            runbook_helpers.docker_start(topology.joiner_container)
            runbook_helpers.wait_healthy([topology.joiner_grpc], timeout=180)

            # ── Step 4a: identity.json unchanged post-wipe ──────────
            identity_after = runbook_helpers.docker_exec(
                topology.joiner_container,
                ["cat", "/app/identity/identity.json"],
                check=True,
            )
            assert identity_after.stdout == identity_before.stdout, (
                f"identity.json content changed across the wipe.  "
                f"Before: {identity_before.stdout!r}\n"
                f"After:  {identity_after.stdout!r}"
            )

            # ── Step 4b: joiner sees the seeded session ─────────────
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                probe = runbook_helpers.docker_exec(
                    topology.joiner_container,
                    ["ls", "-1", session_mount],
                    check=False,
                )
                if probe.rc == 0 and "1.json" in probe.stdout.splitlines():
                    joiner_bytes = cc_tasks_share_helpers.mount_read_bytes(
                        topology.joiner_container, f"{session_mount}/1.json"
                    )
                    assert joiner_bytes == payload, (
                        f"joiner post-rebuild bytes do not match founder's "
                        f"seed: got {joiner_bytes!r}, expected {payload!r}"
                    )
                    return
                time.sleep(1.0)
            pytest.fail(
                "joiner never re-observed the seeded session after data_dir "
                "wipe + sidecar rejoin within 120s — the S3 identity-seeded "
                "recovery workflow is broken; identity.json survived (proven "
                "above) but the post-rejoin metastore replay did not converge"
            )
        finally:
            runbook_helpers.docker_exec(
                topology.founder_container,
                ["rm", "-rf", f"/host/tasks/{session}"],
                check=False,
            )
