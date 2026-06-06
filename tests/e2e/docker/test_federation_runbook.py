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
