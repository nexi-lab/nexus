#!/usr/bin/env python3
"""Join Windows node 1 federation from macOS node 2."""

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

os.environ.pop("NEXUS_PEERS", None)

data_dir = os.path.expanduser("~/.nexus/federation/data")
zones_dir = str(Path(data_dir) / "zones")
Path(zones_dir).mkdir(parents=True, exist_ok=True)
sys.stdout.reconfigure(line_buffering=True)

LEADER_ADDR = "10.99.0.1:2126"
NODE_ID = 2
SELF_ADDR = "10.99.0.2:2126"

print("=" * 60)
print("Nexus Federation Join — Node 2 (macOS)")
print("=" * 60)

from nexus.contracts.constants import ROOT_ZONE_ID  # noqa: E402
from nexus.raft.client import RaftClient  # noqa: E402
from nexus.raft.zone_manager import ZoneManager  # noqa: E402

# Step 1: Create local zone replica (no bootstrap — waits for leader snapshot)
mgr = ZoneManager(node_id=NODE_ID, base_path=zones_dir, bind_addr="0.0.0.0:2126")
store = mgr.join_zone(ROOT_ZONE_ID, peers=[f"1@{LEADER_ADDR}"])

print("  Local zone created (skip_bootstrap)")
print("  gRPC: 0.0.0.0:2126")


# Step 2: Ask leader to add us as Voter
async def request_join():
    client = RaftClient(address=LEADER_ADDR)
    try:
        result = await client.join_zone(
            zone_id=ROOT_ZONE_ID,
            node_id=NODE_ID,
            node_address=SELF_ADDR,
        )
        return result
    finally:
        await client.close()


result = asyncio.run(request_join())
print(f"  JoinZone result: {result}")

if result.get("success"):
    print("=" * 60)
    print("  Federation joined!")
    print(f"  Node ID:   {NODE_ID}")
    print(f"  WireGuard: 10.99.0.{NODE_ID}")
    print(f"  Leader:    {LEADER_ADDR}")
    print("=" * 60)
else:
    print(f"  Join failed: {result.get('error')}")
    mgr.shutdown()
    sys.exit(1)

# Keep running so raft messages can flow
running = True


def shutdown(sig, frame):
    global running
    print("\nShutting down...")
    running = False


signal.signal(signal.SIGINT, shutdown)
try:
    while running:
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    mgr.shutdown()
    print("Node 2 stopped.")
