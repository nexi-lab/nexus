"""End-to-end audit collect/gather workflow.

Tests the full audit-node loop against the live docker federation:
``audit-node`` joins ``corp`` and ``family`` production zones as a
raft learner, the ``AuditNode`` Python service polls each zone's
``/audit/traces/`` DT_STREAM, and records reach the audit-node's
local ``/audit/collect/{source}/traces`` stream.

Topology (extends ``docker-compose.dynamic-federation-test.yml``):

```
nexus-1  (voter, hosts /corp + /family + their /audit/traces)
nexus-2  (voter, raft replication)
witness  (voter, vote-only)
audit-node  ← THIS is the new node — joins both zones as learner +
              runs AuditNode collect loop, accumulates traces in
              /audit/collect/{corp,family}/traces.
```

Strong causal links (per integration-test-generator skill standard):

  step 1: setup_audit_zone() creates /audit zone on audit-node
          and joins corp + family as learners.  Only after this
          succeeds does step 2 have target zones to write to.
  step 2: produce traces in corp + family by mutating files.  The
          audit hook on those zones writes records to
          /{zone}/audit/traces/.
  step 3: audit-node's collect loop drains both source zones into
          /audit/collect/{zone}/traces — the records produced in
          step 2 reach the audit-node.
  step 4: read /audit/collect/{zone}/traces on the audit-node and
          assert every step-2 record landed there in order, with
          per-zone offsets matching the produced count.

Skip reason: requires the docker compose stack from
``docker-compose.dynamic-federation-test.yml``; cannot run in unit
test envs without the federation cluster.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("NEXUS_DOCKER_E2E"),
    reason="Requires docker-compose.dynamic-federation-test.yml stack; "
    "set NEXUS_DOCKER_E2E=1 to enable",
)


@pytest.fixture(scope="module")
def federation_cluster() -> dict[str, str]:
    """Resolve the in-network hostnames the test runner reaches.

    Inside the compose ``test`` service the cluster is reached by
    container hostname — same names operators see when joining the
    network.
    """
    return {
        "nexus_1_grpc": "nexus-1:2028",
        "nexus_2_grpc": "nexus-2:2028",
        "audit_node_grpc": "audit-node:2028",
        "api_key": os.environ.get("NEXUS_API_KEY", "sk-test-dynamic-federation-key"),
    }


@pytest.fixture
def audit_zone_id() -> str:
    return "audit"


@pytest.fixture
def production_zones() -> list[str]:
    return ["corp", "family"]


def _connect_grpc(addr: str, api_key: str) -> Any:
    """Open a NexusVFS gRPC client against ``addr``.

    Imported lazily because the docker-internal ``test`` service is
    the only environment in which the client + the running cluster
    coexist.
    """
    from nexus.client.grpc_client import NexusGrpcClient

    return NexusGrpcClient(addr, auth_token=api_key)


def _produce_audit_records(client: Any, zone_id: str, count: int) -> list[dict[str, object]]:
    """Trigger audit records in ``zone_id`` by writing files.

    Each write fires the AuditHook; the hook serializes an
    ``AuditRecord`` JSON blob and pushes it onto
    ``/{zone}/audit/traces/``.  Returns the path/payload pairs we
    wrote, so the test can correlate against collected records.
    """
    written: list[dict[str, object]] = []
    for i in range(count):
        path = f"/{zone_id}/audit-fixture/file-{i}.txt"
        payload = f"hello-from-{zone_id}-{i}".encode()
        client.sys_write(path, payload)
        written.append({"path": path, "size": len(payload)})
    return written


def _wait_for_collected(
    audit_client: Any,
    audit_zone: str,
    source_zone: str,
    expected_count: int,
    timeout_secs: float = 30.0,
) -> int:
    """Poll the audit-node's local collect stream until it holds
    ``expected_count`` records or the timeout fires.  Returns the
    final count seen."""
    target = f"/{audit_zone}/collect/{source_zone}/traces"
    deadline = time.time() + timeout_secs
    last_seen = 0
    while time.time() < deadline:
        try:
            entries, _new_offset = audit_client.stream_read_batch(target, 0, expected_count + 16)
        except Exception:
            entries = []
        last_seen = len(entries)
        if last_seen >= expected_count:
            return last_seen
        time.sleep(0.5)
    return last_seen


# ── The long-workflow test ─────────────────────────────────────────


def test_audit_node_collects_traces_from_two_production_zones(
    federation_cluster: dict[str, str],
    audit_zone_id: str,
    production_zones: list[str],
) -> None:
    """End-to-end: audit-node joins corp + family as learners, the
    collect loop drains each zone's audit stream into the
    centralized /audit/collect/{zone}/traces store.

    Workflow: setup → produce → collect → verify.
    """
    nexus1 = _connect_grpc(federation_cluster["nexus_1_grpc"], federation_cluster["api_key"])
    audit = _connect_grpc(federation_cluster["audit_node_grpc"], federation_cluster["api_key"])

    # ── Step 1: setup_audit_zone ────────────────────────────────────
    # Create the audit zone on the audit-node and join both
    # production zones as learners.  Both calls are idempotent — a
    # restart of the audit-node re-runs them safely.
    from nexus.services.audit_node import AuditNode

    # ``NexusGrpcClient`` exposes the underlying PyKernel handle on
    # ``_kernel``; ``getattr`` keeps mypy happy without a type ignore.
    audit_kernel = audit._kernel
    node = AuditNode(
        audit_kernel,
        audit_zone_id=audit_zone_id,
        stream_path="/audit/traces/",
        batch_size=64,
        poll_interval_secs=0.5,
    )
    node.bootstrap(production_zones)

    # Audit-node has a checkpoint per source zone — even if the
    # zones already had pre-existing audit records, the offset will
    # be advanced past them after the first drain in step 3.
    assert set(node._checkpoints.keys()) == set(production_zones)

    # Snapshot the pre-step-2 offset so the assertions count only
    # newly-produced records.
    initial_offsets = {zone: node._checkpoints[zone].offset for zone in production_zones}

    # ── Step 2: produce audit traces in both zones ──────────────────
    # Each write fires the AuditHook in the source zone and appends
    # a record to that zone's /audit/traces stream.
    written_corp = _produce_audit_records(nexus1, "corp", count=5)
    written_family = _produce_audit_records(nexus1, "family", count=3)

    assert len(written_corp) == 5
    assert len(written_family) == 3

    # ── Step 3: drive the collect loop ──────────────────────────────
    # Run the loop briefly so it polls each zone and drains the new
    # records into /audit/collect/{zone}/traces.
    loop = asyncio.new_event_loop()
    try:

        async def _run_for_a_while() -> None:
            task = loop.create_task(node.run())
            # Two poll intervals' worth of time + safety margin.
            await asyncio.sleep(2.0)
            node.stop()
            await asyncio.wait_for(task, timeout=5.0)

        loop.run_until_complete(_run_for_a_while())
    finally:
        loop.close()

    # ── Step 4: verify centralized collection ───────────────────────
    # Per-zone counts in the audit-node's local collect stream match
    # what we produced in step 2 (offset-aware so pre-existing audit
    # noise does not skew the assertion).
    corp_count = _wait_for_collected(
        audit,
        audit_zone_id,
        "corp",
        expected_count=initial_offsets["corp"] + 5,
    )
    family_count = _wait_for_collected(
        audit,
        audit_zone_id,
        "family",
        expected_count=initial_offsets["family"] + 3,
    )

    assert corp_count >= initial_offsets["corp"] + 5, (
        f"audit-node should have collected ≥{initial_offsets['corp'] + 5} "
        f"corp traces, got {corp_count}"
    )
    assert family_count >= initial_offsets["family"] + 3, (
        f"audit-node should have collected ≥{initial_offsets['family'] + 3} "
        f"family traces, got {family_count}"
    )

    # Per-zone offsets advanced past the produced records — so a
    # subsequent run resumes without re-collecting.
    assert node._checkpoints["corp"].offset >= initial_offsets["corp"] + 5
    assert node._checkpoints["family"].offset >= initial_offsets["family"] + 3

    # The offsets are persisted in the audit-node's local zone, so
    # the next AuditNode instance reads them back.
    persisted_corp = audit.sys_read(f"/{audit_zone_id}/collect/corp/offset")
    persisted_family = audit.sys_read(f"/{audit_zone_id}/collect/family/offset")
    assert json.loads(persisted_corp)["offset"] == node._checkpoints["corp"].offset
    assert json.loads(persisted_family)["offset"] == node._checkpoints["family"].offset

    # Spot-check that step-2 file paths actually appear in collected
    # records — the strongest assertion that the data really flowed.
    target_corp = f"/{audit_zone_id}/collect/corp/traces"
    entries_corp, _ = audit.stream_read_batch(
        target_corp,
        initial_offsets["corp"],
        len(written_corp),
    )
    raw_paths = {json.loads(raw).get("path", "") for raw in entries_corp}
    for w in written_corp:
        assert w["path"] in raw_paths, (
            f"corp write to {w['path']} missing from audit-node collect stream; saw {raw_paths}"
        )
