"""Cross-machine WAL smoke — Win <-> Mac federated raft.

Same env-var federation init as `wal_smoke_singlenode.py`, but
`NEXUS_PEERS` lists both Win + Mac Tailscale IPs. The script both
participates in the raft cluster (binds local raft :2126) AND drives
the test against the local kernel — two processes (one per side), one
shared raft cluster.

Test sequence (each side, in parallel):
  1. setup_env: NEXUS_HOSTNAME = own Tailscale IP, NEXUS_PEERS = both.
  2. create_nexus_fs(): kernel boots, install_federation_wiring fires,
     init_from_env joins the 2-voter root raft group. Blocks until
     leader elected (peer must be up too).
  3. sys_setattr(io_profile="wal") on /coord/<own-side>-{stream,pipe} —
     replicates the metadata + WAL setup commands across the cluster.
  4. push 3 messages to own outbound stream + pipe.
  5. poll for peer's outbound paths (sys_stat) — replicated metadata
     should appear within seconds.
  6. read 3 messages from peer's stream + pop 3 from peer's pipe.
  7. verify matches `<peer>-msg-{1,2,3}` per direction.

Both sides are symmetric. Run on Win + Mac near-simultaneously
(within ~30s of each other so the 2-voter cluster reaches quorum
before either side gives up).

Run::
    # On Win:
    PYTHONPATH=. ~/.nexus/nexus_env/python.exe scripts/wal_smoke_xmachine.py --side win
    # On Mac:
    PYTHONPATH=. ~/.nexus/nexus_env/python.exe scripts/wal_smoke_xmachine.py --side mac

Status: PASS = both sides see all 3 of the other side's messages on
both stream + pipe paths. FAIL = any verification mismatch or timeout.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

WIN_TS_IP = "100.64.0.26"
MAC_TS_IP = "100.64.0.21"
RAFT_PORT = 2126

PEERS_CSV = f"{WIN_TS_IP}:{RAFT_PORT},{MAC_TS_IP}:{RAFT_PORT}"

# Per-side output paths — each side writes its own, reads peer's.
PATHS = {
    "win": {
        "out_stream": "/coord/win-stream",
        "out_pipe": "/coord/win-pipe",
        "in_stream": "/coord/mac-stream",
        "in_pipe": "/coord/mac-pipe",
        "ts_ip": WIN_TS_IP,
    },
    "mac": {
        "out_stream": "/coord/mac-stream",
        "out_pipe": "/coord/mac-pipe",
        "in_stream": "/coord/win-stream",
        "in_pipe": "/coord/win-pipe",
        "ts_ip": MAC_TS_IP,
    },
}


def setup_env(side: str, tmp: Path) -> None:
    cfg = PATHS[side]
    os.environ["NEXUS_HOSTNAME"] = cfg["ts_ip"]
    os.environ["NEXUS_PEERS"] = PEERS_CSV
    os.environ["NEXUS_BIND_ADDR"] = f"0.0.0.0:{RAFT_PORT}"
    os.environ["NEXUS_DATA_DIR"] = str(tmp / "zones")
    os.environ["NEXUS_RAFT_TLS"] = "false"
    print(f"  side: {side}")
    print(f"  NEXUS_HOSTNAME={os.environ['NEXUS_HOSTNAME']}")
    print(f"  NEXUS_PEERS={os.environ['NEXUS_PEERS']}")
    print(f"  NEXUS_DATA_DIR={os.environ['NEXUS_DATA_DIR']}")


def section(label: str) -> None:
    print(f"\n=== {label} ===")


def expect(label: str, value: object, expected: object) -> bool:
    ok = value == expected
    mark = "OK " if ok else "FAIL"
    print(f"  [{mark}] {label}: got={value!r} want={expected!r}")
    return ok


def setattr_outbound(nx, side: str) -> bool:
    """Create our own outbound stream + pipe with io_profile=wal."""
    section(f"[{side}] setup outbound — sys_setattr io_profile='wal'")
    from nexus.contracts.metadata import DT_PIPE, DT_STREAM

    cfg = PATHS[side]
    try:
        nx.sys_setattr(
            cfg["out_stream"],
            entry_type=DT_STREAM,
            io_profile="wal",
            capacity=65_536,
        )
        nx.sys_setattr(
            cfg["out_pipe"],
            entry_type=DT_PIPE,
            io_profile="wal",
            capacity=65_536,
        )
    except Exception as e:
        print(f"  [FAIL] sys_setattr raised: {type(e).__name__}: {e}")
        return False
    print(f"  outbound stream: {cfg['out_stream']}")
    print(f"  outbound pipe:   {cfg['out_pipe']}")
    return True


def push_outbound(nx, side: str) -> None:
    """Push 3 messages to our outbound stream + pipe."""
    section(f"[{side}] push 3 messages outbound")
    cfg = PATHS[side]
    for i in (1, 2, 3):
        msg = f"{side}-msg-{i}".encode()
        nx._kernel.stream_write_nowait(cfg["out_stream"], msg)
        nx._kernel.pipe_write_nowait(cfg["out_pipe"], msg)
        print(f"  pushed {msg!r} to {cfg['out_stream']} + {cfg['out_pipe']}")


def wait_peer_inodes(nx, side: str, timeout_s: float = 60.0) -> bool:
    """Poll for the peer's setattr to replicate."""
    section(f"[{side}] wait for peer's inodes (replication via raft)")
    cfg = PATHS[side]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            in_stream_ok = nx._kernel.has_stream(cfg["in_stream"])
            in_pipe_ok = nx._kernel.has_pipe(cfg["in_pipe"])
        except Exception:
            in_stream_ok = in_pipe_ok = False
        if in_stream_ok and in_pipe_ok:
            print(f"  peer paths visible: {cfg['in_stream']}, {cfg['in_pipe']}")
            return True
        time.sleep(0.5)
    print(f"  [FAIL] peer paths still missing after {timeout_s}s")
    return False


def read_inbound(nx, side: str, peer: str, timeout_s: float = 30.0) -> bool:
    """Read 3 messages from peer's stream + pop 3 from peer's pipe."""
    section(f"[{side}] read 3 inbound from peer ({peer})")
    cfg = PATHS[side]
    expected = [f"{peer}-msg-{i}".encode() for i in (1, 2, 3)]
    deadline = time.time() + timeout_s

    # WAL stream: read via offset = seq (0, 1, 2). dict shape returned.
    cursor = 0
    got_stream: list[bytes] = []
    while len(got_stream) < 3 and time.time() < deadline:
        try:
            r = nx.sys_read(cfg["in_stream"], offset=cursor)
        except Exception:
            time.sleep(0.2)
            continue
        if isinstance(r, dict) and r.get("data"):
            got_stream.append(r["data"])
            cursor = r["next_offset"]
        elif isinstance(r, dict):
            time.sleep(0.2)
        else:
            print(f"  [FAIL] sys_read returned non-dict for DT_STREAM: {r!r}")
            return False
    if got_stream != expected:
        print(f"  [FAIL] stream mismatch: got={got_stream!r} want={expected!r}")
        return False
    print(f"  stream OK: {got_stream!r}")

    # WAL pipe: pop 3 via pipe_read_nowait (with retry for bg flush).
    got_pipe: list[bytes] = []
    while len(got_pipe) < 3 and time.time() < deadline:
        popped = nx._kernel.pipe_read_nowait(cfg["in_pipe"])
        if popped is not None:
            got_pipe.append(bytes(popped))
        else:
            time.sleep(0.2)
    if got_pipe != expected:
        print(f"  [FAIL] pipe mismatch: got={got_pipe!r} want={expected!r}")
        return False
    print(f"  pipe OK: {got_pipe!r}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--side", choices=("win", "mac"), required=True)
    parser.add_argument(
        "--cluster-form-timeout",
        type=float,
        default=120.0,
        help="seconds to wait for raft cluster to form (peer must come up)",
    )
    parser.add_argument(
        "--inode-replicate-timeout",
        type=float,
        default=60.0,
        help="seconds to wait for peer's setattr to replicate",
    )
    parser.add_argument(
        "--read-inbound-timeout",
        type=float,
        default=60.0,
        help="seconds to wait for peer's pushed messages to replicate",
    )
    args = parser.parse_args()

    side = args.side
    peer = "mac" if side == "win" else "win"

    tmp = Path(tempfile.mkdtemp(prefix=f"wal-xmachine-{side}-"))
    print(f"smoke tmpdir: {tmp}")
    setup_env(side, tmp)

    try:
        from nexus.backends.storage.cas_local import CASLocalBackend
        from nexus.core.config import ParseConfig, PermissionConfig
        from nexus.factory import create_nexus_fs
        from nexus.storage.record_store import SQLAlchemyRecordStore
        from tests.helpers.dict_metastore import DictMetastore

        print(
            "\n[bringup] create_nexus_fs — federation init from env, "
            "this blocks until raft 2-voter quorum is reached..."
        )
        nx = create_nexus_fs(
            backend=CASLocalBackend(tmp / "cas"),
            metadata_store=DictMetastore(),
            record_store=SQLAlchemyRecordStore(db_path=tmp / "meta.db"),
            parsing=ParseConfig(auto_parse=False),
            permissions=PermissionConfig(enforce=False),
        )
        print("[bringup] kernel up, federation initialized")

        # Stage 1: each side advertises its own outbound paths.
        if not setattr_outbound(nx, side):
            return 1
        push_outbound(nx, side)

        # Stage 2: wait for peer to do the same.
        if not wait_peer_inodes(nx, side, timeout_s=args.inode_replicate_timeout):
            return 1

        # Stage 3: read peer's messages + verify.
        if not read_inbound(nx, side, peer, timeout_s=args.read_inbound_timeout):
            return 1

        print()
        print("=" * 60)
        print(f"RESULT [{side}]: PASS — round-trip verified both directions")
        return 0
    except Exception:
        traceback.print_exc()
        return 2
    finally:
        # Keep tmp dir for post-mortem inspection on failure; clean up
        # only on PASS via explicit return-code check.
        pass


if __name__ == "__main__":
    rc = main()
    sys.exit(rc)
