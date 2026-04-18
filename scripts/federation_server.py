#!/usr/bin/env python3
"""Nexus Federation Server — Node 1 (Windows).

Binds gRPC to WireGuard IP only (10.99.0.1:2126) to avoid public exposure.
"""

import os
import signal
import socket
import sys
import time
from pathlib import Path

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.raft.zone_manager import ZoneManager

# ── Configuration ──────────────────────────────────────────────────────────────
WIREGUARD_IP = "10.99.0.1"
GRPC_PORT = 2126
BIND_ADDR = f"{WIREGUARD_IP}:{GRPC_PORT}"
# ───────────────────────────────────────────────────────────────────────────────


def main() -> None:
    os.environ.pop("NEXUS_PEERS", None)
    sys.stdout.reconfigure(line_buffering=True)

    data_dir = os.path.expanduser("~/.nexus/federation/data")
    zones_dir = str(Path(data_dir) / "zones")
    Path(zones_dir).mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("Nexus Federation Server — Node 1")
    print("=" * 60)

    mgr = ZoneManager(
        hostname=socket.gethostname(), base_path=zones_dir, peers=[], bind_addr=BIND_ADDR
    )
    store = mgr.bootstrap(root_zone_id=ROOT_ZONE_ID)

    print(f"  is_leader: {store._engine.is_leader()}")
    print(f"  gRPC:      {BIND_ADDR}")
    print(f"  Data dir:  {zones_dir}")
    print(f"  WireGuard: {WIREGUARD_IP}")
    print("=" * 60)
    print("Waiting for macOS node 2 to join... (Ctrl+C to stop)")

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
        print("Server stopped.")


if __name__ == "__main__":
    main()
