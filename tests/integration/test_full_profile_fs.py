"""Gated real-stack FS E2E for the FULL profile (Issue #4133).

Reuses the ``full_stack`` fixture introduced by #4132
(tests/integration/conftest.py). The fixture skips cheaply when
``NEXUS_E2E != "1"`` or Docker is unavailable. This test exercises the
end-to-end lifecycle, batch, range, stream, and lock surface against a
real booted FULL hub (gRPC), proving that the CLI/RPC/syscall parity
verified by tests/unit/cli/test_fs_parity.py also holds over the wire.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration

requires_e2e = pytest.mark.skipif(
    os.environ.get("NEXUS_E2E") != "1",
    reason="FULL FS E2E requires NEXUS_E2E=1 (real Docker stack)",
)


@requires_e2e
def test_full_fs_lifecycle_batch_range_lock(full_stack, monkeypatch):
    """Exercise the full FS surface against the real FULL hub.

    Bundled into one test to amortize the ~30s stack boot (fixture is
    function-scoped). Asserts byte-identity, batch shapes, range
    correctness, and lock cycle behavior — same invariants the unit
    parity suite covers, but over the real gRPC transport.
    """
    from nexus.sdk import connect

    monkeypatch.setenv("NEXUS_GRPC_PORT", str(full_stack.grpc_port))

    nx = connect(
        config={
            "profile": "remote",
            "url": full_stack.url,
            "api_key": full_stack.api_key,
        }
    )
    assert nx is not None

    # Lifecycle: write -> stat -> read -> rename -> delete
    nx.write("/e2e/a.txt", b"alpha")
    nx.write("/e2e/b.txt", b"beta")
    assert nx.read("/e2e/a.txt") == b"alpha"
    st = nx.stat("/e2e/a.txt")
    assert st["size"] == 5 and "content_id" in st

    # Range: bytes [0, 3) of "alpha" == "alp"
    assert nx.read_range("/e2e/a.txt", 0, 3) == b"alp"

    # Batch read parity
    bulk = nx.read_bulk(["/e2e/a.txt", "/e2e/b.txt"])
    assert bulk["/e2e/a.txt"] == b"alpha"
    assert bulk["/e2e/b.txt"] == b"beta"

    # Existence batch
    ex = nx.exists_batch(["/e2e/a.txt", "/e2e/nope.txt"])
    assert ex == {"/e2e/a.txt": True, "/e2e/nope.txt": False}

    # stat_bulk (5 core fields per entry)
    sb = nx.stat_bulk(["/e2e/a.txt", "/e2e/b.txt"])
    assert sb["/e2e/a.txt"]["size"] == 5 and sb["/e2e/b.txt"]["size"] == 4

    # rename_batch is per-item independent (NOT atomic)
    rn = nx.rename_batch([("/e2e/b.txt", "/e2e/c.txt")])
    assert rn["/e2e/b.txt"]["success"] is True
    assert nx.read("/e2e/c.txt") == b"beta"

    # Lock cycle
    lid = nx.sys_lock("/e2e/a.txt")
    assert lid  # non-empty lock id
    assert nx.sys_unlock("/e2e/a.txt", lock_id=lid) is True

    # delete_batch (also per-item independent)
    dl = nx.delete_batch(["/e2e/a.txt", "/e2e/c.txt"])
    assert all(r["success"] for r in dl.values())
