#!/usr/bin/env python3
"""End-to-end Raft log replication test.

Dynamically discovers the current leader via get_cluster_info(), writes
FileMetadata to it, then reads from the follower (read_from_leader=False)
to verify Raft log replication across the WireGuard tunnel.

Usage:
    python scripts/replication_test.py
"""

import asyncio
import sys
import time
import uuid

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.metadata import FileMetadata
from nexus.raft.client import RaftClient

# ── Configuration ──────────────────────────────────────────────────────────────
ALL_NODES = {
    1: "10.99.0.1:2126",  # Windows
    2: "10.99.0.2:2126",  # macOS
}
REPLICATION_WAIT_S = 2.0  # seconds to wait for replication to propagate
# ───────────────────────────────────────────────────────────────────────────────

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"


def step(n: int, msg: str) -> None:
    print(f"\nStep {n}: {msg}")


async def run_test() -> bool:
    uid = uuid.uuid4().hex[:8]
    test_path = f"/replication-test-{uid}.txt"

    print("=" * 60)
    print("Nexus Federation — Raft Replication Test")
    print("=" * 60)

    # ── Step 1: Cluster health + discover leader ────────────────────────────
    step(1, "Check cluster info, discover leader")

    leader_addr: str | None = None
    follower_addr: str | None = None

    for node_id, addr in ALL_NODES.items():
        async with RaftClient(addr, zone_id=ROOT_ZONE_ID) as c:
            info = await c.get_cluster_info()
        lid = info.get("leader_id")
        term = info.get("term")
        is_leader = info.get("is_leader")
        print(f"  Node {node_id} ({addr}): leader_id={lid} term={term} is_leader={is_leader}")
        if is_leader:
            leader_addr = addr
        else:
            follower_addr = addr

    if not leader_addr or not follower_addr:
        print(f"  [{FAIL}] Could not identify both a leader and a follower.")
        print(f"          leader={leader_addr}  follower={follower_addr}")
        return False

    print(f"\n  → Leader:   {leader_addr}")
    print(f"  → Follower: {follower_addr}")
    print(f"  → Test key: {test_path}")

    # ── Step 2: Write to leader ─────────────────────────────────────────────
    step(2, f"Write FileMetadata to leader ({leader_addr})")

    meta = FileMetadata(
        path=test_path,
        backend_name="test",
        physical_path=f"/data{test_path}",
        size=1234,
        etag=uid,
    )

    async with RaftClient(leader_addr, zone_id=ROOT_ZONE_ID) as c:
        ok = await c.put_metadata(meta)
        if not ok:
            print(f"  [{FAIL}] put_metadata returned False")
            return False
        print(f"  [ok] put_metadata succeeded (size={meta.size} etag={uid})")

    # ── Step 3: Wait for replication ───────────────────────────────────────
    step(3, f"Wait {REPLICATION_WAIT_S}s for Raft log replication to follower")
    time.sleep(REPLICATION_WAIT_S)

    # ── Step 4: Read from follower (local read, no leader redirect) ─────────
    step(4, f"Read from follower ({follower_addr}) — read_from_leader=False")

    async with RaftClient(follower_addr, zone_id=ROOT_ZONE_ID) as c:
        result = await c.get_metadata(test_path, read_from_leader=False)

    if result is None:
        print(f"  [{FAIL}] get_metadata returned None — data not replicated yet")
        return False

    if result.path != test_path:
        print(f"  [{FAIL}] path mismatch: expected {test_path!r}, got {result.path!r}")
        return False

    if result.size != meta.size:
        print(f"  [{FAIL}] size mismatch: expected {meta.size}, got {result.size}")
        return False

    if result.etag != uid:
        print(f"  [{FAIL}] etag mismatch: expected {uid!r}, got {result.etag!r}")
        return False

    print(f"  [ok] path={result.path!r}  size={result.size}  etag={result.etag!r}")

    return True


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)

    try:
        passed = asyncio.run(run_test())
    except Exception as e:
        print(f"\n  [ERROR] {e}")
        passed = False

    print("\n" + "=" * 60)
    if passed:
        print(f"  Result: [{PASS}] Raft replication is working correctly.")
    else:
        print(f"  Result: [{FAIL}] Replication test failed — check logs above.")
    print("=" * 60 + "\n")

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
