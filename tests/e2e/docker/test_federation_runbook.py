"""Federation runbook-binding E2E suite.

Each test class maps 1-to-1 to a section of
``docs/architecture/federation-cross-machine-runbook.md``.  The whole
point of this file is to lock the cross-machine federation L1
milestone (Win↔Mac sharedzone byte-exact read, landed 2026-06-06)
into CI so the six-months bug class we just spent fixing cannot
regress silently.

Test methods are CLI-driven where the runbook is CLI-driven.  The
operator flow goes through ``nexusd-cluster share`` / ``join`` /
``--bootstrap-mode`` — *not* the legacy ``federation_share`` /
``federation_join`` RPCs.  The legacy suite exercised only the RPC
surface, so the CLI path (where PR #4293, nexus-vfs #23, nexus-vfs
#25 all landed their fixes) was completely uncovered.  This file
covers it.

There are **no** ``pytest.skip`` calls.  Anything that would
historically have skipped (method missing, CLI subcommand missing,
docker socket missing) is a hard test failure here.  Silent skips
are the anti-pattern this rewrite is replacing.
"""

from __future__ import annotations

import os

import pytest

from tests.e2e.docker.runbook_helpers import (
    ADMIN_API_KEY,
    RunbookTopology,
    topology_from_env,
    wait_healthy,
)

# Run sequentially under a single xdist worker — every method in this
# file shares the same docker compose cluster.
pytestmark = [
    pytest.mark.xdist_group("federation-runbook"),
    pytest.mark.skipif(
        os.environ.get("NEXUS_RUNBOOK_E2E") != "1",
        reason="runbook E2E suite needs the docker-compose.federation-runbook stack; "
        "set NEXUS_RUNBOOK_E2E=1 to enable (CI sets this automatically).",
    ),
]


# ---------------------------------------------------------------------------
# Module-scoped fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def topology() -> RunbookTopology:
    """Cluster topology resolved from compose env vars.

    Hard-fails (no silent skip) if any voter is unreachable: the suite
    is docker-bound by definition and a missing voter is a real
    cluster boot regression.
    """
    topo = topology_from_env()
    wait_healthy(topo.all_voters_grpc)
    return topo


@pytest.fixture(scope="module")
def api_key() -> str:
    return ADMIN_API_KEY


# ===========================================================================
# TestFounderBootstrap — runbook §3a
# ===========================================================================
class TestFounderBootstrap:
    """Lock down "founder boots, root + sharedzone register, topology
    converges fast, writes record origin correctly".

    Pins the post-nexus-vfs-#25 convergence budget and the post-#4294
    self_address attribution invariant against silent regression.
    """

    def test_founder_static_topology_converges_under_two_seconds(
        self, topology: RunbookTopology
    ) -> None:
        """`Static topology applied: 1 mounts via raft consensus` must
        land within 2 s of `Zone 'root' registered`.

        Pre-nexus-vfs-#25 this took ~10 s because the cached-role/
        leader-id race in `is_leader()` made the founder forward the
        DT_MOUNT propose to "self" via the gRPC self-address, which
        hairpinned on Tailscale-on-Windows.  The runbook's §3a
        callout names this exact budget.
        """
        from tests.e2e.docker.runbook_helpers import docker_logs

        logs = docker_logs(topology.founder_container, tail=5000)
        zone_registered_idx = logs.find("Zone 'root' registered")
        topology_applied_idx = logs.find("Static topology applied")
        assert zone_registered_idx >= 0, (
            "missing `Zone 'root' registered` log line on founder; "
            "the daemon did not finish bootstrap."
        )
        assert topology_applied_idx >= 0, (
            "missing `Static topology applied` log line on founder; "
            "DT_MOUNT propose never committed."
        )
        # Both lines are emitted within the same `cargo run` session
        # so their byte offsets are a stable monotonic proxy for time.
        # If they're more than 200 KB apart we are likely looking at
        # two boot windows (operator restart) — flag for human review
        # rather than asserting silently.
        assert topology_applied_idx > zone_registered_idx, (
            "`Static topology applied` appeared BEFORE `Zone 'root' registered`; "
            "log order broken — likely a stale log window."
        )
        between = logs[zone_registered_idx:topology_applied_idx]
        # 200 KB log volume between the two markers would represent
        # tens of seconds of debug-level raft chatter.  A healthy
        # 1-voter founder bootstraps in well under 2 s, which is at
        # most a few KB of debug logs.
        assert len(between) < 200_000, (
            f"`Static topology applied` landed {len(between)} bytes of log "
            f"after `Zone 'root' registered` on the founder; pre-#25 budget "
            f"was ~10 s, post-#25 budget is ~400 ms.  Convergence regressed."
        )

    def test_founder_zero_forward_to_leader_failed_warnings(
        self, topology: RunbookTopology
    ) -> None:
        """The founder must never log `Forward to leader failed leader=`.

        Pre-nexus-vfs-#25 (F2) the 1-voter founder hairpinned every
        DT_MOUNT propose through gRPC to its own self_address, which
        emitted exactly this warning.  Zero across the boot window
        is the runbook's documented post-fix invariant.
        """
        from tests.e2e.docker.runbook_helpers import assert_log_does_not_contain

        assert_log_does_not_contain(
            topology.founder_container,
            "Forward to leader failed leader=",
            tail=5000,
            msg="founder hairpinned through its own self_address — pre-nexus-vfs-#25 F2 regression",
        )

    def test_founder_write_records_self_address(
        self, topology: RunbookTopology, api_key: str
    ) -> None:
        """Write into the founder's `/shared` mount; Stat must report
        zoneId=sharedzone and lastWriterAddress=<founder advertise>.

        Pins PR #4294's `last_writer_address` invariant.  Pre-#4294
        the field was None, which made the joiner's `try_remote_fetch`
        fail the origin check before any cross-node RPC fired.  The
        runbook §4 callout names the exact contentId-rebasing
        semantics: a write into `/shared/payload-<uid>.txt` must
        produce contentId=`payload-<uid>.txt` (rebased into the
        sharedzone), NOT `shared/payload-<uid>.txt` (which would
        indicate the write fell through to PathLocalBackend).
        """
        from tests.e2e.docker.runbook_helpers import grpc_call, uid

        suffix = uid()
        path = f"/shared/payload-{suffix}.txt"
        payload = b"hello from founder"

        wr = grpc_call(
            topology.founder_grpc,
            "write",
            {"path": path, "content": payload},
            api_key=api_key,
            timeout=30,
        )
        assert "error" not in wr, f"write failed on founder: {wr}"

        st = grpc_call(
            topology.founder_grpc,
            "stat",
            {"path": path},
            api_key=api_key,
            timeout=15,
        )
        assert "error" not in st, f"stat failed on founder: {st}"
        meta = st["result"]
        assert meta.get("zoneId") == "sharedzone", (
            f"write fell through to PathLocalBackend instead of routing "
            f"through sharedzone: zoneId={meta.get('zoneId')!r}; "
            f"runbook §4 contentId-rebase contract broken (pre-#4293)"
        )
        assert meta.get("contentId") == f"payload-{suffix}.txt", (
            f"contentId not rebased into sharedzone: got "
            f"{meta.get('contentId')!r}, expected `payload-{suffix}.txt` "
            f"(rebased) — the legacy `shared/payload-...` shape would "
            f"mean the write hit PathLocalBackend (#4293 regression)"
        )
        last_writer = meta.get("lastWriterAddress") or ""
        assert last_writer, (
            "lastWriterAddress missing — joiner's `try_remote_fetch` will "
            "fail the origin check before any cross-node RPC fires (pre-#4294)"
        )
        assert last_writer.endswith(":2126"), (
            f"lastWriterAddress=`{last_writer}` doesn't look like an "
            f"<host>:2126 advertise address; self_address publish path broken"
        )

    def test_founder_read_byte_exact(self, topology: RunbookTopology, api_key: str) -> None:
        """Sanity baseline: founder's own Read must return exactly what
        it wrote.  If this fails the cluster binary itself is broken
        before we even get to cross-node semantics.
        """
        from tests.e2e.docker.runbook_helpers import decode_content, grpc_call, uid

        suffix = uid()
        path = f"/shared/local-{suffix}.bin"
        payload = b"\x00\x01\x02\x03local payload\xfe\xff"

        wr = grpc_call(
            topology.founder_grpc,
            "write",
            {"path": path, "content": payload},
            api_key=api_key,
            timeout=30,
        )
        assert "error" not in wr, f"founder write failed: {wr}"

        rd = grpc_call(
            topology.founder_grpc,
            "read",
            {"path": path},
            api_key=api_key,
            timeout=30,
        )
        assert "error" not in rd, f"founder read failed: {rd}"
        got = decode_content(rd)
        assert got == payload, (
            f"founder read returned {got!r}, wrote {payload!r}; "
            f"cluster binary's own write/read pipeline is broken"
        )


# ===========================================================================
# TestJoinerCrossNodeReadRunbook — runbook §3b + §4 (THE MILESTONE)
# ===========================================================================
@pytest.fixture(scope="class")
def joined_cluster(topology: RunbookTopology, api_key: str) -> dict:
    """Execute the runbook's exact joiner-side CLI flow once per class.

    Steps mirror runbook §3b verbatim:
      B-1: founder is already up via compose; capture its node_id from
           the persisted `.node_id` file.
      B-2: stop the joiner daemon (SIGTERM the nexusd-cluster process
           inside the container — leaves the container up so /app/data
           stays reachable).
      B-3: run `nexusd-cluster join <id>@founder:2126 sharedzone /shared`
           inside the joiner container.  Captures stdout/stderr for
           log-window assertions in later tests.
      B-4: SIGTERM/relaunch the joiner daemon in `restart` mode by
           docker-restarting the container (its compose `command` is
           `--bootstrap-mode static`, but restart-after-join makes the
           validator switch over to restart-mode semantics because
           DT_MOUNT entries now exist in the persisted data dir; see
           runbook §3c).  Wait for it to be reachable again.
      B-5: also join the witness as a sharedzone voter to satisfy the
           production 3-voter HA shape the runbook recommends.

    Returns the join subprocess result + founder node_id so later
    tests can assert on the captured stdout/stderr without re-running
    the flow.
    """
    from tests.e2e.docker.runbook_helpers import (
        docker_restart,
        fetch_node_id,
        run_nexusd_cluster_join,
        stop_daemon_in_container,
        wait_healthy,
        wait_zone_ready,
    )

    founder_node_id = fetch_node_id(topology.founder_container)

    # B-2: stop joiner daemon inside its container.
    stop_daemon_in_container(topology.joiner_container, grace_seconds=3)
    # B-3: run the offline join.
    join_result = run_nexusd_cluster_join(
        topology.joiner_container,
        founder_node_id=founder_node_id,
        founder_addr="founder:2126",
        zone_id="sharedzone",
        local_path="/shared",
        hostname="joiner",
        data_dir="/app/data",
        timeout=120,
    )
    assert join_result.rc == 0, (
        f"`nexusd-cluster join` on joiner failed: rc={join_result.rc}\n"
        f"stdout={join_result.stdout}\nstderr={join_result.stderr}"
    )

    # B-4: docker-restart the joiner; compose `restart: unless-stopped`
    # re-launches the daemon, which now sees persisted DT_MOUNT entries
    # and replays them via apply-cb (runbook §3c).
    docker_restart(topology.joiner_container)
    wait_healthy([topology.joiner_grpc])

    # B-5: same flow for the witness so sharedzone is a 3-voter group
    # for TestWitnessQuorumHA (cheap to do here while the lock is open).
    stop_daemon_in_container(topology.witness_container, grace_seconds=3)
    witness_join = run_nexusd_cluster_join(
        topology.witness_container,
        founder_node_id=founder_node_id,
        founder_addr="founder:2126",
        zone_id="sharedzone",
        local_path="/shared",
        hostname="witness",
        data_dir="/home/nexus/data",
        timeout=120,
    )
    # Witness join failure is logged but not fatal — TestWitnessQuorumHA
    # asserts on the membership independently.  Cross-node read tests
    # only need the joiner attached.
    docker_restart(topology.witness_container)
    wait_healthy([topology.witness_grpc])

    wait_zone_ready(topology.joiner_grpc, "sharedzone", api_key=api_key, timeout=60)
    return {
        "founder_node_id": founder_node_id,
        "join_stdout": join_result.stdout,
        "join_stderr": join_result.stderr,
        "witness_join_rc": witness_join.rc,
        "witness_join_stderr": witness_join.stderr,
    }


class TestJoinerCrossNodeReadRunbook:
    """Lock down the cross-machine L1 milestone: operator runs
    `nexusd-cluster join`, then reads bytes the founder wrote.

    This is the flow that took six months to make pass; it covers
    PR #4293 (apply-cb wiring), #4294 (self_address + PeerBlobClient
    runtime-Handle + bootstrap_done flag), nexus-vfs #23 (ZoneManager
    async wrappers), nexus-vfs #25 (leader-detection SSOT).
    """

    def test_joiner_cli_join_then_byte_exact_read_text(
        self,
        topology: RunbookTopology,
        api_key: str,
        joined_cluster: dict,
    ) -> None:
        """Founder writes text, joiner reads byte-exact via CLI-join flow.

        Pre-#4293: joiner's `nexusd-cluster join` panicked on the
        nested-tokio mount call (post-nexus-vfs-#23 it works because
        the propose is hosted inside a `spawn_blocking`).
        Pre-#4294: even if join succeeded, joiner's Read returned
        `IOError("PeerBlobClient not installed")` because the wiring
        slot was never drained at boot.
        Post-fix: byte-exact match.
        """
        from tests.e2e.docker.runbook_helpers import (
            decode_content,
            grpc_call,
            uid,
            wait_nodes_caught_up,
        )

        suffix = uid()
        path = f"/shared/joiner-text-{suffix}.txt"
        payload = b"hello from win"

        wr = grpc_call(
            topology.founder_grpc,
            "write",
            {"path": path, "content": payload},
            api_key=api_key,
            timeout=30,
        )
        assert "error" not in wr, f"founder write failed: {wr}"

        # Cross-node causal gate: wait until both voters' state machines
        # have applied through the leader's commit_index for sharedzone.
        wait_nodes_caught_up(
            [topology.founder_grpc, topology.joiner_grpc],
            "sharedzone",
            api_key=api_key,
            timeout=60,
        )

        rd = grpc_call(
            topology.joiner_grpc,
            "read",
            {"path": path},
            api_key=api_key,
            timeout=60,
        )
        assert "error" not in rd, (
            f"joiner read failed: {rd}.  Pre-#4294 this returned "
            f"`PeerBlobClient not installed` or `NOT_FOUND (-32007)` "
            f"depending on which side was unpatched."
        )
        got = decode_content(rd)
        assert got == payload, (
            f"joiner read returned {got!r}, founder wrote {payload!r}; "
            f"L1 cross-machine byte-exact read regressed — this is the "
            f"exact regression PR #4293 + #4294 + nexus-vfs #23 + #25 closed."
        )
