"""Single-node WAL smoke — Win local validation before cross-machine bring-up.

Runs `create_nexus_fs` in-process with `NEXUS_PEERS` set to just this node
(quorum=1 single-voter raft) so the WAL stream/pipe path through
`MetaStore::append_stream_entry` / `get_stream_entry` lights up without
needing a real cluster.

If this passes, WalStreamCore + WalPipeCore + the raft state-machine
table for stream entries all work end-to-end on develop tip. Cross-machine
smoke is then just "spin up two of these with NEXUS_PEERS pointing at each
other and verify replication".

Run::
    PYTHONPATH=. ~/.nexus/nexus_env/python.exe scripts/wal_smoke_singlenode.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

WIN_TS_IP = "100.64.0.26"  # Tailscale IP, matches `tailscale status`
RAFT_PORT = 2126


def setup_env(tmp: Path) -> None:
    """Configure single-voter federation before importing nexus."""
    os.environ["NEXUS_HOSTNAME"] = WIN_TS_IP
    os.environ["NEXUS_PEERS"] = f"{WIN_TS_IP}:{RAFT_PORT}"
    os.environ["NEXUS_BIND_ADDR"] = f"0.0.0.0:{RAFT_PORT}"
    os.environ["NEXUS_DATA_DIR"] = str(tmp / "zones")
    os.environ["NEXUS_RAFT_TLS"] = "false"
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


def smoke_wal_stream(nx) -> bool:
    """sys_setattr(io_profile='wal') for DT_STREAM should round-trip
    through raft + return dict on sys_read."""
    section("WAL DT_STREAM — io_profile='wal' over single-voter raft")
    from nexus.contracts.metadata import DT_STREAM

    path = "/coord-stream"
    try:
        result = nx.sys_setattr(path, entry_type=DT_STREAM, io_profile="wal", capacity=65_536)
    except Exception as e:
        print(f"  [FAIL] sys_setattr raised: {type(e).__name__}: {e}")
        return False
    print(f"  sys_setattr returned: {result}")

    nx._kernel.stream_write_nowait(path, b"hello")
    nx._kernel.stream_write_nowait(path, b"world!!")

    # WAL stream uses (seq, data) keying, not byte-offset. seq=0 is the
    # first entry; next_offset = seq + 1 in the WAL backend.
    r1 = nx._kernel.stream_read_at(path, 0)
    if r1 is None:
        # Async flush race — bg thread hasn't committed yet. Retry briefly.
        import time

        for _ in range(40):
            time.sleep(0.05)
            r1 = nx._kernel.stream_read_at(path, 0)
            if r1 is not None:
                break
    if r1 is None:
        print("  [FAIL] stream_read_at(0) returned None after 2s wait")
        return False
    data1, next1 = r1
    if not expect("first payload", bytes(data1), b"hello"):
        return False

    r2 = nx._kernel.stream_read_at(path, next1)
    if r2 is None:
        print(f"  [FAIL] stream_read_at({next1}) returned None")
        return False
    data2, _ = r2
    return expect("second payload", bytes(data2), b"world!!")


def smoke_wal_pipe(nx) -> bool:
    """sys_setattr(io_profile='wal') for DT_PIPE — push/pop FIFO."""
    section("WAL DT_PIPE — io_profile='wal' over single-voter raft")
    from nexus.contracts.metadata import DT_PIPE

    path = "/coord-pipe"
    try:
        result = nx.sys_setattr(path, entry_type=DT_PIPE, io_profile="wal", capacity=65_536)
    except Exception as e:
        print(f"  [FAIL] sys_setattr raised: {type(e).__name__}: {e}")
        return False
    print(f"  sys_setattr returned: {result}")

    for msg in (b"msg-1", b"msg-2", b"msg-3"):
        nx._kernel.pipe_write_nowait(path, msg)

    # Brief wait for bg flush (single-voter raft commits sync, but the
    # WalStreamCore flush thread is still async).
    import time

    time.sleep(0.2)

    pop1 = nx._kernel.pipe_read_nowait(path)
    if not expect("FIFO pop 1", bytes(pop1) if pop1 else None, b"msg-1"):
        return False
    pop2 = nx._kernel.pipe_read_nowait(path)
    if not expect("FIFO pop 2", bytes(pop2) if pop2 else None, b"msg-2"):
        return False
    pop3 = nx._kernel.pipe_read_nowait(path)
    if not expect("FIFO pop 3", bytes(pop3) if pop3 else None, b"msg-3"):
        return False

    pop_empty = nx._kernel.pipe_read_nowait(path)
    return expect("empty pop returns None", pop_empty, None)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="wal-smoke-"))
    print(f"smoke tmpdir: {tmp}")
    setup_env(tmp)

    try:
        from nexus.backends.storage.cas_local import CASLocalBackend
        from nexus.core.config import ParseConfig, PermissionConfig
        from nexus.factory import create_nexus_fs
        from nexus.storage.record_store import SQLAlchemyRecordStore
        from tests.helpers.dict_metastore import DictMetastore

        nx = create_nexus_fs(
            backend=CASLocalBackend(tmp / "cas"),
            metadata_store=DictMetastore(),
            record_store=SQLAlchemyRecordStore(db_path=tmp / "meta.db"),
            parsing=ParseConfig(auto_parse=False),
            permissions=PermissionConfig(enforce=False),
        )

        # Verify federation actually initialized (zone_manager_arc must
        # return Some). If init_from_env didn't fire, the wal branch
        # will raise "requires federation" — same skip path as before.
        results = [smoke_wal_stream(nx), smoke_wal_pipe(nx)]
        ok = all(results)
        print()
        print("=" * 60)
        print("RESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    except Exception:
        traceback.print_exc()
        return 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
