"""Gated real-stack FS E2E for the FULL profile (Issue #4133).

Reuses the ``full_stack_tolerant`` fixture introduced by #4132
(tests/integration/conftest.py). The fixture skips cheaply when
``NEXUS_E2E != "1"`` or Docker is unavailable. This test exercises the
end-to-end FS surface against a real booted FULL hub over its HTTP
JSON-RPC wire (``/api/v2/files/*`` typed routes + ``/api/nfs/{method}``
generic dispatcher), proving that the CLI/RPC/syscall parity verified
by tests/unit/cli/test_fs_parity.py also holds end-to-end over the
network.

Note on transport: the ``shared`` preset's nexus container exposes
the HTTP server only; gRPC is not started inside the container even
though the host port is mapped. HTTP carries the same dispatch
contract (same dispatch_method() / rpc_expose registry), so this
test validates the same CLI↔RPC↔kernel parity invariants over a
real wire.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.integration

requires_e2e = pytest.mark.skipif(
    os.environ.get("NEXUS_E2E") != "1",
    reason="FULL FS E2E requires NEXUS_E2E=1 (real Docker stack)",
)


def _rpc(base: str, key: str, method: str, params: dict) -> dict:
    req = urllib.request.Request(
        f"{base}/api/nfs/{method}",
        data=json.dumps(params).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise AssertionError(f"{method} HTTP {exc.code}: {exc.read()[:300]!r}") from exc
    if "error" in payload and payload["error"]:
        raise AssertionError(f"{method} RPC error: {payload['error']}")
    return payload["result"]


def _files_write(base: str, key: str, path: str, data: bytes) -> dict:
    req = urllib.request.Request(
        f"{base}/api/v2/files/write",
        data=json.dumps(
            {
                "path": path,
                "content": base64.b64encode(data).decode(),
                "encoding": "base64",
            }
        ).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _bytes_from_rpc(val):
    """RPC byte payloads come back as ``{"__type__":"bytes","data":"<b64>"}``."""
    if isinstance(val, dict) and val.get("__type__") == "bytes":
        return base64.b64decode(val["data"])
    if isinstance(val, str):
        return val.encode()
    raise AssertionError(f"unexpected RPC byte shape: {val!r}")


@requires_e2e
def test_full_fs_lifecycle_batch_range_lock(full_stack_tolerant):
    """Exercise the full FS surface against the real FULL hub over HTTP.

    Bundled into one test to amortize the ~60s stack boot (fixture is
    function-scoped). Asserts byte-identity, batch shapes, range
    correctness, and lock cycle behavior — same invariants the unit
    parity suite covers, but over the real HTTP JSON-RPC transport
    (``/api/v2/files/write`` for typed lifecycle + ``/api/nfs/{method}``
    for generic dispatch).
    """
    fs = full_stack_tolerant
    base = fs.url.replace("localhost", "127.0.0.1").rstrip("/")
    key = fs.api_key

    # ---- Lifecycle: write -> stat -> read -> read_range --------------------
    w1 = _files_write(base, key, "/e2e/a.txt", b"alpha")
    assert w1["size"] == 5
    w2 = _files_write(base, key, "/e2e/b.txt", b"beta")
    assert w2["size"] == 4

    st = _rpc(base, key, "stat", {"path": "/e2e/a.txt"})
    assert st["size"] == 5 and "content_id" in st

    rng = _rpc(base, key, "read_range", {"path": "/e2e/a.txt", "start": 0, "end": 3})
    assert _bytes_from_rpc(rng) == b"alp"

    # ---- Batch surface -----------------------------------------------------
    bulk = _rpc(base, key, "read_bulk", {"paths": ["/e2e/a.txt", "/e2e/b.txt"]})
    assert _bytes_from_rpc(bulk["/e2e/a.txt"]) == b"alpha"
    assert _bytes_from_rpc(bulk["/e2e/b.txt"]) == b"beta"

    ex = _rpc(base, key, "exists_batch", {"paths": ["/e2e/a.txt", "/e2e/nope.txt"]})
    assert ex == {"/e2e/a.txt": True, "/e2e/nope.txt": False}

    meta = _rpc(base, key, "metadata_batch", {"paths": ["/e2e/a.txt"]})
    assert meta["/e2e/a.txt"]["size"] == 5
    assert "mime_type" in meta["/e2e/a.txt"]

    # ---- Mutation: rename_batch (per-item independent) ---------------------
    # rename_batch takes list of (old, new) 2-element lists/tuples.
    rn = _rpc(
        base,
        key,
        "rename_batch",
        {"renames": [["/e2e/b.txt", "/e2e/c.txt"]]},
    )
    assert rn["/e2e/b.txt"]["success"] is True
    rd = _rpc(base, key, "read", {"path": "/e2e/c.txt"})
    assert _bytes_from_rpc(rd) == b"beta"

    # ---- Lock cycle --------------------------------------------------------
    lid = _rpc(base, key, "sys_lock", {"path": "/e2e/a.txt"})
    assert lid  # non-empty lock id
    unlocked = _rpc(base, key, "sys_unlock", {"path": "/e2e/a.txt", "lock_id": lid})
    # sys_unlock returns ``True`` from the kernel, but the HTTP RPC layer
    # auto-wraps scalar bools into ``{"released": True}``. Accept either.
    assert unlocked is True or (isinstance(unlocked, dict) and unlocked.get("released") is True)

    # ---- delete_batch (also per-item independent) --------------------------
    dl = _rpc(base, key, "delete_batch", {"paths": ["/e2e/a.txt", "/e2e/c.txt"]})
    assert all(r["success"] for r in dl.values())

    # ---- Admin-only refusal verified over the wire -------------------------
    # backfill_directory_index / flush_write_observer carry admin_only=True.
    # The fixture's api_key is the registered admin key — we expect success
    # there (the unit suite already verifies non-admin rejection via
    # dispatch_method directly, where we can inject a non-admin context).
    bf = _rpc(base, key, "backfill_directory_index", {"prefix": "/"})
    assert "entries_created" in bf
    fw = _rpc(base, key, "flush_write_observer", {})
    assert "flushed" in fw
