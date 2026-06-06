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
