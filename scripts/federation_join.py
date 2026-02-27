#!/usr/bin/env python3
"""Join Windows node 1 federation from macOS node 2."""

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.raft.client import RaftClient
from nexus.raft.zone_manager import ZoneManager

# ── Configuration ──────────────────────────────────────────────────────────────
NODE_ID = 2
WIREGUARD_IP = "10.99.0.2"
GRPC_PORT = 2126
BIND_ADDR = f"{WIREGUARD_IP}:{GRPC_PORT}"
LEADER_ADDR = "10.99.0.1:2126"
# ───────────────────────────────────────────────────────────────────────────────


async def request_join(mgr: ZoneManager) -> dict:  # noqa: ARG001
    client = RaftClient(address=LEADER_ADDR)
    await client.connect()
    try:
        return await client.join_zone(
            zone_id=ROOT_ZONE_ID,
            node_id=NODE_ID,
            node_address=BIND_ADDR,
        )
    finally:
        await client.close()


def main() -> None:
    os.environ.pop("NEXUS_PEERS", None)
    sys.stdout.reconfigure(line_buffering=True)

    data_dir = os.path.expanduser("~/.nexus/federation/data")
    zones_dir = str(Path(data_dir) / "zones")
    Path(zones_dir).mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("Nexus Federation Join — Node 2 (macOS)")
    print("=" * 60)

    # Step 1: Create local zone replica (no bootstrap — waits for leader snapshot)
    mgr = ZoneManager(node_id=NODE_ID, base_path=zones_dir, bind_addr=BIND_ADDR)
    mgr.join_zone(ROOT_ZONE_ID, peers=[f"1@{LEADER_ADDR}"])

    print("  Local zone created (skip_bootstrap)")
    print(f"  gRPC: {BIND_ADDR}")

    # Step 2: Ask leader to add us as Voter
    result = asyncio.run(request_join(mgr))
    print(f"  JoinZone result: {result}")

    if not result.get("success"):
        print(f"  Join failed: {result.get('error')}")
        mgr.shutdown()
        sys.exit(1)

    print("=" * 60)
    print("  Federation joined!")
    print(f"  Node ID:   {NODE_ID}")
    print(f"  WireGuard: {WIREGUARD_IP}")
    print(f"  Leader:    {LEADER_ADDR}")
    print("=" * 60)

    # Keep running so raft messages can flow
    running = True

    def shutdown(sig, frame):
        nonlocal running
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


if __name__ == "__main__":
    main()
