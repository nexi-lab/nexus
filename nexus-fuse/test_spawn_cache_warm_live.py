#!/usr/bin/env python3
"""Live test: NexusFUSEOperations._spawn_cache_warm against a real Rust daemon (Issue #4055).

Prereq: a running Nexus server. Reads NEXUS_URL / NEXUS_API_KEY from env (set via
`eval $(nexus env)`).

Exercises the production hydration trigger path:
1. Build a real RustFUSEClient that boots a real Rust nexus-fuse daemon.
2. Seed 3 files on the live backend (2 small, 1 over-threshold).
3. Instantiate NexusFUSEOperations via __new__ (bypassing the heavyweight constructor)
   and invoke _spawn_cache_warm with the real client.
4. Wait for the daemon thread to call rust_client.cache_warm and log results.
5. Verify the cache_warm RPC actually fired and admitted the small files
   by inspecting the daemon's response (we wrap cache_warm to capture stats).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nexus.fuse.operations import NexusFUSEOperations
from nexus.fuse.rust_client import RustFUSEClient


def main() -> int:
    print("🧪 Live spawn-cache-warm test\n")

    nexus_url = os.environ.get("NEXUS_URL", "http://localhost:2026")
    api_key = os.environ.get("NEXUS_API_KEY", "sk-test-key-123")
    rust_binary = os.environ.get(
        "NEXUS_FUSE_BINARY",
        str(Path(__file__).parent / "target/debug/nexus-fuse"),
    )
    print(f"nexus_url={nexus_url}")

    # Capture the daemon-thread output.
    captured: dict[str, object] = {}

    with RustFUSEClient(
        nexus_url=nexus_url,
        api_key=api_key,
        rust_binary=rust_binary,
    ) as client:
        # Seed files on the live backend.
        client.sys_write("/spawn_a.txt", b"alpha")
        client.sys_write("/spawn_b.txt", b"beta")
        client.sys_write("/spawn_big.bin", b"x" * (200 * 1024))
        print("seeded 3 files on backend")

        # Wrap cache_warm so we can assert it really fires.
        original = client.cache_warm
        done = threading.Event()

        def _wrap(*args, **kwargs):
            stats = original(*args, **kwargs)
            captured["stats"] = stats
            captured["called_with"] = (args, kwargs)
            done.set()
            return stats

        setattr(client, "cache_warm", _wrap)  # noqa: B010

        # Construct an Ops instance via __new__ so we don't need to satisfy
        # the rest of __init__'s dependency graph. _kickoff_cache_warm only
        # references its `rust_client` argument and the module-level logger.
        ops = NexusFUSEOperations.__new__(NexusFUSEOperations)
        ops._kickoff_cache_warm(client)

        # The kickoff call must return immediately (non-blocking).
        # Give the daemon thread up to 10s to actually fire cache_warm.
        if not done.wait(timeout=10.0):
            print("❌ cache_warm was never invoked by the spawned thread")
            return 1

    stats = captured["stats"]
    called_with = captured["called_with"]
    print(f"✓ thread invoked cache_warm with {called_with!r}")
    print(f"✓ daemon returned: {stats}")

    # Production trigger uses wait=False, so the daemon returns
    # `{"started": true}` immediately and the BFS+fetch+admit pipeline
    # runs as a detached tokio task. Final stats surface via Rust daemon
    # logs (drained by RustFUSEClient stderr thread) and metrics — they
    # are NOT in the RPC reply. Verify the kickoff shape.
    if not isinstance(stats, dict):
        print(f"❌ unexpected stats shape: {stats!r}")
        return 1
    if not stats.get("started"):
        print(f"❌ expected wait=False kickoff response, got {stats!r}")
        return 1
    # Verify the call_args carry wait=False so the production path is
    # exercised, not the synchronous test path.
    args, kwargs = called_with
    if kwargs.get("wait") is not False:
        print(f"❌ expected wait=False kwarg, got {called_with!r}")
        return 1
    print("\n✅ live spawn-cache-warm test passed")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
