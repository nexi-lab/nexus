#!/usr/bin/env python3
"""End-to-end test for Issue #4058 vectored read_batch.

Connects to a running Nexus stack and exercises:
  1. POST /api/v2/files/batch/read — wraps our modified Python read_batch
     (Tasks 9 + 10 PyO3 + wrapper).
  2. POST /api/v2/files/batch-read — legacy read_bulk back-compat.
  3. gRPC BatchRead RPC directly (Tasks 11 + 12).

Run after `nexus up --build` + `eval $(nexus env)`.
Exit 0 = all green.
"""

from __future__ import annotations

import base64
import os
import sys
import time

import grpc
import httpx

from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"


def fail(msg: str) -> None:
    print(f"{RED}FAIL: {msg}{RESET}")
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"{GREEN}PASS: {msg}{RESET}")


def info(msg: str) -> None:
    print(f"{YELLOW}INFO: {msg}{RESET}")


def env() -> dict[str, str]:
    required = ["NEXUS_URL", "NEXUS_GRPC_HOST", "NEXUS_API_KEY"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        fail(f"missing env vars: {missing} — run `eval $(nexus env)` first")
    return {k: os.environ[k] for k in required}


# ── HTTP helpers ─────────────────────────────────────────────────────


def http_write(c: httpx.Client, path: str, content: bytes) -> None:
    """POST /api/v2/files/write — uses base64 for binary safety."""
    body = {
        "path": path,
        "content": base64.b64encode(content).decode("ascii"),
        "encoding": "base64",
    }
    r = c.post("/api/v2/files/write", json=body, timeout=30)
    if r.status_code >= 400:
        fail(f"write {path} -> HTTP {r.status_code}: {r.text[:200]}")


def http_delete(c: httpx.Client, path: str) -> None:
    c.delete("/api/v2/files/delete", params={"path": path}, timeout=30)


# ── 1. POST /api/v2/files/batch/read (our wrapped read_batch) ───────


def test_http_batch_read(c: httpx.Client) -> None:
    info("Testing POST /api/v2/files/batch/read (read_batch wrapper)")

    payloads = {
        "/e2e_4058_a.txt": b"alpha",
        "/e2e_4058_b.txt": b"beta_payload",
        "/e2e_4058_c.txt": b"gamma______",
    }
    for path, content in payloads.items():
        http_write(c, path, content)

    # 1.1 — happy path
    r = c.post(
        "/api/v2/files/batch/read",
        json={"paths": list(payloads.keys()), "partial": False},
        timeout=30,
    )
    if r.status_code >= 400:
        fail(f"batch/read -> HTTP {r.status_code}: {r.text[:300]}")
    body = r.json()
    items = body.get("results", body) if isinstance(body, dict) else body
    if not isinstance(items, list) or len(items) != 3:
        fail(f"batch/read shape wrong (expected 3 items): {body!r}")
    for i, (path, content) in enumerate(payloads.items()):
        item = items[i]
        if item.get("type") == "error" or item.get("error"):
            fail(f"[batch/read/{path}] error: {item}")
        item_content = item.get("content_base64") or item.get("content")
        item_bytes = base64.b64decode(item_content) if item_content else b""
        if item_bytes != content:
            fail(f"[batch/read/{path}] content mismatch: got {item_bytes!r}, want {content!r}")
    ok("HTTP batch/read: 3-item happy path returns correct bytes in input order")

    # 1.2 — partial mode with missing path
    r2 = c.post(
        "/api/v2/files/batch/read",
        json={"paths": ["/e2e_4058_a.txt", "/e2e_4058_missing.txt"], "partial": True},
        timeout=30,
    )
    if r2.status_code >= 400:
        fail(f"batch/read partial -> HTTP {r2.status_code}: {r2.text[:300]}")
    body2 = r2.json()
    items2 = body2.get("results", body2) if isinstance(body2, dict) else body2
    if len(items2) != 2:
        fail(f"partial mode: expected 2 items, got {len(items2)}: {body2!r}")
    if items2[1].get("error") != "not_found":
        fail(f"partial mode: missing path didn't return error=not_found: {items2[1]!r}")
    ok("HTTP batch/read: partial mode reports per-item not_found")

    # 1.3 — strict mode raises on missing
    r3 = c.post(
        "/api/v2/files/batch/read",
        json={"paths": ["/e2e_4058_a.txt", "/e2e_4058_missing.txt"], "partial": False},
        timeout=30,
    )
    if r3.status_code < 400:
        fail(f"strict mode should fail on missing path, got HTTP {r3.status_code}: {r3.text[:200]}")
    ok("HTTP batch/read: strict mode rejects missing path with non-2xx")

    # Cleanup
    for path in payloads:
        http_delete(c, path)


# ── 2. POST /api/v2/files/batch-read (legacy read_bulk back-compat) ──


def test_http_batch_read_legacy(c: httpx.Client) -> None:
    info("Testing POST /api/v2/files/batch-read (legacy read_bulk)")

    paths = [f"/e2e_bulk_{i}.txt" for i in range(5)]
    for i, p in enumerate(paths):
        http_write(c, p, f"bulk-{i}".encode())

    r = c.post("/api/v2/files/batch-read", json={"paths": paths}, timeout=30)
    if r.status_code >= 400:
        fail(f"batch-read -> HTTP {r.status_code}: {r.text[:300]}")
    body = r.json()
    # Could be {"files": {...}} or {"results": [...]} — accept either; just verify each path
    # appears with the right content somehow.
    json_str = r.text
    for i, p in enumerate(paths):
        want = f"bulk-{i}"
        # base64 encoding check
        b64 = base64.b64encode(want.encode()).decode()
        if want not in json_str and b64 not in json_str:
            fail(f"[batch-read/{p}] payload missing from response: {json_str[:200]}")
    ok("HTTP batch-read (legacy read_bulk): 5 files round-tripped")

    for p in paths:
        http_delete(c, p)


# ── 3. gRPC BatchRead direct ─────────────────────────────────────────


def test_grpc_batch_read(env_vars: dict[str, str], c: httpx.Client) -> None:
    info("Testing gRPC BatchRead RPC directly")

    payloads = {
        "/e2e_grpc_a.txt": b"alpha",
        "/e2e_grpc_b.txt": b"beta_payload",
        "/e2e_grpc_c.txt": b"gamma______",
    }
    for path, content in payloads.items():
        http_write(c, path, content)

    channel = grpc.insecure_channel(env_vars["NEXUS_GRPC_HOST"])
    stub = vfs_pb2_grpc.NexusVFSServiceStub(channel)
    api_key = env_vars["NEXUS_API_KEY"]

    try:
        # 3.1 — basic
        req = vfs_pb2.BatchReadRequest(
            auth_token=api_key,
            items=[vfs_pb2.BatchReadItemRequest(path=p, offset=0) for p in payloads],
        )
        resp = stub.BatchRead(req, timeout=30)
        if len(resp.results) != 3:
            fail(f"gRPC BatchRead: expected 3 results, got {len(resp.results)}")
        for i, (path, content) in enumerate(payloads.items()):
            r = resp.results[i]
            if r.is_error:
                fail(f"[gRPC/{path}] error: {r.error_payload}")
            if r.content != content:
                fail(f"[gRPC/{path}] content mismatch: got {r.content!r}, want {content!r}")
        ok("gRPC BatchRead: 3-item happy path, order preserved")

        # 3.2 — mixed hit / not_found / slice
        req2 = vfs_pb2.BatchReadRequest(
            auth_token=api_key,
            items=[
                vfs_pb2.BatchReadItemRequest(path="/e2e_grpc_a.txt", offset=0),
                vfs_pb2.BatchReadItemRequest(path="/e2e_grpc_missing.txt", offset=0),
                vfs_pb2.BatchReadItemRequest(path="/e2e_grpc_b.txt", offset=2, length=4),  # "ta_p"
            ],
        )
        resp2 = stub.BatchRead(req2, timeout=30)
        if resp2.results[0].is_error or resp2.results[0].content != b"alpha":
            fail(
                f"[gRPC/mixed/0] error={resp2.results[0].is_error} content={resp2.results[0].content!r}"
            )
        if not resp2.results[1].is_error:
            fail("[gRPC/mixed/1] expected error for missing")
        if resp2.results[2].is_error or resp2.results[2].content != b"ta_p":
            fail(
                f"[gRPC/mixed/2] slice mismatch: error={resp2.results[2].is_error} content={resp2.results[2].content!r}"
            )
        ok("gRPC BatchRead: mixed hit/miss/slice with per-item error mapping")

        # 3.3 — empty
        resp3 = stub.BatchRead(vfs_pb2.BatchReadRequest(auth_token=api_key, items=[]), timeout=30)
        if len(resp3.results) != 0:
            fail(f"gRPC BatchRead empty: expected 0 results, got {len(resp3.results)}")
        ok("gRPC BatchRead: empty input")

        # 3.4 — coalesce same path 5 times
        resp4 = stub.BatchRead(
            vfs_pb2.BatchReadRequest(
                auth_token=api_key,
                items=[vfs_pb2.BatchReadItemRequest(path="/e2e_grpc_a.txt", offset=0)] * 5,
            ),
            timeout=30,
        )
        if len(resp4.results) != 5:
            fail(f"gRPC BatchRead coalesce: expected 5 results, got {len(resp4.results)}")
        for i, r in enumerate(resp4.results):
            if r.is_error or r.content != b"alpha":
                fail(f"[gRPC/coalesce/{i}] mismatch: error={r.is_error} content={r.content!r}")
        ok("gRPC BatchRead: 5 reads of same path coalesce correctly")
    finally:
        channel.close()
        for path in payloads:
            http_delete(c, path)


# ── Main ──────────────────────────────────────────────────────────────


def main() -> int:
    env_vars = env()
    info(f"NEXUS_URL  = {env_vars['NEXUS_URL']}")
    info(f"NEXUS_GRPC = {env_vars['NEXUS_GRPC_HOST']}")
    info(f"NEXUS_KEY  = ...{env_vars['NEXUS_API_KEY'][-8:]}")

    # Health probe
    deadline = time.time() + 30
    healthy = False
    while time.time() < deadline:
        try:
            r = httpx.get(f"{env_vars['NEXUS_URL']}/health", timeout=3)
            if r.status_code == 200:
                healthy = True
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    if not healthy:
        fail("stack not reachable within 30s")
    ok("Stack /health responds 200")

    c = httpx.Client(
        base_url=env_vars["NEXUS_URL"],
        headers={"Authorization": f"Bearer {env_vars['NEXUS_API_KEY']}"},
    )
    try:
        test_http_batch_read(c)
        test_http_batch_read_legacy(c)
        test_grpc_batch_read(env_vars, c)
    finally:
        c.close()

    print(f"\n{GREEN}All e2e checks passed.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
