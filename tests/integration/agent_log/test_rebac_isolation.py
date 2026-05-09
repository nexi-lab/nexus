"""End-to-end-ish isolation tests for issue #4081 agent_log.

Phase B (runtime wiring of /.activity/ mount + ReBAC + agent onboarding) is
deferred to a follow-up. These tests exercise what the Phase A building
blocks guarantee:

- AgentLogBrick issues grants that, when honored by the ReBAC evaluator,
  isolate each agent to their own log file.
- The store's read_path returns only the bytes for the agent whose id is
  in the path, regardless of which agent_id was used during writes by other
  agents.
- The recursion guard in the sink prevents writes to /.activity/ from
  generating further records.

When Phase B lands, these tests can be promoted to drive the full FS +
ReBAC stack.
"""

import json

import pytest

from nexus.bricks.agent_log.brick import AgentLogBrick
from nexus.contracts.protocols.activity import (
    EventKind,
    Result,
)
from nexus.services.activity.agent_log_store import MemoryBackend
from nexus.services.activity.events import ActivityEvent, Actor
from nexus.services.activity.sinks.jsonl import JsonlActivitySink


async def _noop_mount(*, path, backend):
    return None


def _evt(*, kind, agent, ts="2026-05-09T12:00:00.000Z", meta=None, result=Result.OK, latency_ms=10):
    return ActivityEvent(
        id=f"e-{agent}-{kind.value}",
        ts=ts,
        kind=kind,
        result=result,
        latency_ms=latency_ms,
        actor=Actor(agent=agent),
        meta=meta or {},
    )


@pytest.mark.asyncio
async def test_brick_grant_template_isolates_agents():
    """Each agent gets a grant that only matches their own path glob."""
    grants = []

    def fake_grant(*, subject, relation, object):  # noqa: A002
        grants.append((subject, relation, object))

    brick = AgentLogBrick(add_mount=_noop_mount, add_rebac_grant=fake_grant, store=object())
    await brick.startup(agent_ids=["alice", "bob"])

    # Verify by simulating ReBAC's path-glob match: alice's grant must NOT
    # match a path under bob's filename, and vice versa.
    def grant_allows(subject: str, target_path: str) -> bool:
        for s, _, o in grants:
            if s != subject:
                continue
            assert o.startswith("path:"), o
            pattern = o[len("path:") :]
            # Convert single-segment glob to a literal prefix/suffix check.
            assert pattern.count("*") == 1
            prefix, suffix = pattern.split("*", 1)
            if target_path.startswith(prefix) and target_path.endswith(suffix):
                # No slashes in the wildcard segment.
                middle = (
                    target_path[len(prefix) : -len(suffix)]
                    if suffix
                    else target_path[len(prefix) :]
                )
                if "/" not in middle:
                    return True
        return False

    # Alice can read her own file across any date.
    assert grant_allows("agent:alice", "/.activity/2026-05-09/alice.jsonl")
    assert grant_allows("agent:alice", "/.activity/2026-05-10/alice.jsonl")
    # Alice cannot read bob's file.
    assert not grant_allows("agent:alice", "/.activity/2026-05-09/bob.jsonl")
    # Bob cannot read alice's file.
    assert not grant_allows("agent:bob", "/.activity/2026-05-09/alice.jsonl")
    # No grant subject for "carol" yet — denied for both.
    assert not grant_allows("agent:carol", "/.activity/2026-05-09/alice.jsonl")
    assert not grant_allows("agent:carol", "/.activity/2026-05-09/carol.jsonl")


@pytest.mark.asyncio
async def test_store_isolation_via_path():
    """Reads from /.activity/{date}/{X}.jsonl return only X's bytes,
    even when many agents have written to the same store."""
    store = MemoryBackend(cap_bytes=4096)
    sink = JsonlActivitySink(store=store)

    # Two agents emit ops on the same day.
    e_alice = _evt(
        kind=EventKind.OP, agent="alice", meta={"op": "read", "path": "/s3/alice/foo", "bytes": 100}
    )
    e_bob = _evt(
        kind=EventKind.OP, agent="bob", meta={"op": "write", "path": "/s3/bob/bar", "bytes": 200}
    )
    await sink.write_batch([e_alice, e_bob])

    raw_alice = store.read_path("/.activity/2026-05-09/alice.jsonl")
    raw_bob = store.read_path("/.activity/2026-05-09/bob.jsonl")

    rec_alice = [json.loads(line) for line in raw_alice.strip().split(b"\n") if line]
    rec_bob = [json.loads(line) for line in raw_bob.strip().split(b"\n") if line]

    # Alice's file contains only alice's record.
    assert len(rec_alice) == 1
    assert rec_alice[0]["path"] == "/s3/alice/foo"
    # Bob's file contains only bob's record.
    assert len(rec_bob) == 1
    assert rec_bob[0]["path"] == "/s3/bob/bar"


@pytest.mark.asyncio
async def test_unknown_agent_path_returns_empty():
    """A path for an agent who never wrote returns no bytes — the absence
    of a buffer is the read-time analog of the ReBAC denial path."""
    store = MemoryBackend(cap_bytes=4096)
    sink = JsonlActivitySink(store=store)

    e = _evt(kind=EventKind.OP, agent="alice", meta={"op": "read", "path": "/x", "bytes": 1})
    await sink.write_batch([e])

    assert store.read_path("/.activity/2026-05-09/ghost.jsonl") == b""


@pytest.mark.asyncio
async def test_recursion_guard_drops_self_observation():
    """Writes attempted at /.activity/ paths produce no further records."""
    store = MemoryBackend(cap_bytes=4096)
    sink = JsonlActivitySink(store=store)

    # An attempted self-write that somehow reached the sink (via dispatch
    # post-hook, etc.) — sink filters it out.
    bad = _evt(
        kind=EventKind.OP,
        agent="alice",
        meta={"op": "write", "path": "/.activity/2026-05-09/alice.jsonl", "bytes": 5},
    )
    good = _evt(
        kind=EventKind.OP, agent="alice", meta={"op": "read", "path": "/local/x.txt", "bytes": 5}
    )
    await sink.write_batch([bad, good])

    raw = store.read_path("/.activity/2026-05-09/alice.jsonl")
    recs = [json.loads(line) for line in raw.strip().split(b"\n") if line]
    assert len(recs) == 1
    assert recs[0]["path"] == "/local/x.txt"
    assert sink.recursion_skipped == 1
