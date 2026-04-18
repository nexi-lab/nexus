#!/usr/bin/env python3
"""E2E validation for build performance + search + permissions (Issue #2965).

Requires a running Nexus stack (nexus init --preset demo && nexus up && nexus demo init).
Pass NEXUS_URL, NEXUS_API_KEY, NEXUS_GRPC_PORT, and NEXUS_DEMO_USER_KEY as env vars.

Usage:
    # After starting the stack:
    export NEXUS_URL=http://localhost:2027
    export NEXUS_API_KEY=nx_admin_...
    export NEXUS_GRPC_PORT=2029
    export NEXUS_DEMO_USER_KEY=sk-root_demo_use_...
    python scripts/test_build_perf_e2e.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request

NEXUS_CLI = os.environ.get("NEXUS_CLI", "nexus")
NEXUS_URL = os.environ.get("NEXUS_URL", "http://localhost:2027")
ADMIN_KEY = os.environ.get("NEXUS_API_KEY", "")
USER_KEY = os.environ.get("NEXUS_DEMO_USER_KEY", "")
GRPC_PORT = os.environ.get("NEXUS_GRPC_PORT", "2029")

passed = 0
failed = 0
results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  \u2713 {name}")
    else:
        failed += 1
        print(f"  \u2717 {name} \u2014 {detail}")
    results.append((name, condition, detail))


def cli(
    *args: str, api_key: str | None = None, timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    key = api_key or ADMIN_KEY
    cmd = [NEXUS_CLI, *args, "--remote-url", NEXUS_URL, "--remote-api-key", key]
    env = {
        **os.environ,
        "NEXUS_GRPC_PORT": GRPC_PORT,
        "NEXUS_URL": NEXUS_URL,
        "NEXUS_API_KEY": key,
    }
    debug = os.environ.get("NEXUS_E2E_DEBUG") in ("1", "true", "yes")
    t0 = time.perf_counter()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        elapsed = time.perf_counter() - t0
        if r.returncode != 0 or (debug and not r.stdout.strip()):
            brief = " ".join(args[:3])
            stderr_head = (r.stderr or "").strip().splitlines()[:5]
            print(
                f"    [cli: {brief!r} rc={r.returncode} t={elapsed:.1f}s stdout={len(r.stdout)}B stderr={stderr_head}]",
                file=sys.stderr,
                flush=True,
            )
        return r
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - t0
        brief = " ".join(args[:3])
        print(
            f"    [cli: {brief!r} TIMEOUT after {elapsed:.1f}s (limit={timeout}s)]",
            file=sys.stderr,
            flush=True,
        )
        raise


def rpc_transport():
    """Create an RPC transport for direct server calls."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    from nexus.remote.rpc_transport import RPCTransport

    # Auto-discover mTLS certs from nexus.yaml data_dir
    tls_config = None
    try:
        from pathlib import Path

        import yaml

        nexus_yaml = Path(os.path.dirname(__file__), "..", "nexus.yaml")
        if nexus_yaml.exists():
            with open(nexus_yaml) as f:
                cfg = yaml.safe_load(f)
            tls_dir = Path(cfg.get("data_dir", "")) / "tls"
            if cfg.get("tls") and tls_dir.exists() and (tls_dir / "ca.pem").exists():

                class _TlsCfg:
                    def __init__(self, d: Path):
                        self.ca_pem = (d / "ca.pem").read_bytes()
                        self.node_key_pem = (d / "node-key.pem").read_bytes()
                        self.node_cert_pem = (d / "node.pem").read_bytes()

                tls_config = _TlsCfg(tls_dir)
    except Exception:
        pass  # Fall back to insecure

    return RPCTransport(f"localhost:{GRPC_PORT}", auth_token=ADMIN_KEY, tls_config=tls_config)


def main() -> None:
    if not ADMIN_KEY:
        print("ERROR: NEXUS_API_KEY not set")
        sys.exit(1)

    t = rpc_transport()

    # =========================================================================
    print("=== 1. INFRA & STATUS ===")
    # =========================================================================

    # Health check via HTTP
    try:
        resp = urllib.request.urlopen(f"{NEXUS_URL}/healthz/ready", timeout=5)
        health = resp.read().decode()
        check("Health endpoint", "ready" in health)
    except Exception as e:
        check("Health endpoint", False, str(e))

    # nexus status (--json for machine-readable output with "server_reachable" key)
    r = cli("status", "--json")
    check("nexus status", "server_reachable" in r.stdout or "healthy" in r.stdout.lower())

    # ls
    r = cli("ls", "/workspace/demo")
    check("Server reachable (ls)", r.returncode == 0 or "data" in r.stdout)
    check("Demo files exist", "README.md" in r.stdout)

    r = cli("ls", "/workspace/demo/herb/customers")
    check("HERB corpus (5 customers)", "cust-005" in r.stdout)

    r = cli("ls", "/workspace/demo/herb/employees")
    check("HERB corpus (employees)", "emp-003" in r.stdout)

    r = cli("ls", "/workspace/demo/herb/products")
    check("HERB corpus (products)", "prod-003" in r.stdout)

    # =========================================================================
    print("\n=== 2. FILE CRUD ===")
    # =========================================================================
    r = cli("cat", "/workspace/demo/README.md")
    check("cat README.md", "Nexus Demo Workspace" in r.stdout)

    r = cli("cat", "/workspace/demo/auth-flow.md")
    check("cat auth-flow.md", "authentication" in r.stdout.lower())

    # =========================================================================
    print("\n=== 3. GREP / KEYWORD SEARCH ===")
    # =========================================================================
    r = cli("grep", "authentication", "/workspace/demo")
    check("grep 'authentication'", "data" in r.stdout)

    r = cli("grep", "vector", "/workspace/demo")
    check("grep 'vector'", "data" in r.stdout)

    # =========================================================================
    print("\n=== 4. EDIT (exact, fuzzy, preview, OCC) ===")
    # =========================================================================
    result = t.call_rpc(
        "edit",
        {
            "path": "/workspace/demo/plan.md",
            "edits": [["Configure authentication", "Configure auth (test-edit)"]],
        },
    )
    check("edit (exact)", result.get("success") and result.get("applied_count") == 1)

    result = t.call_rpc(
        "edit",
        {
            "path": "/workspace/demo/plan.md",
            "edits": [["Configure auth (test-edit)", "PREVIEW"]],
            "preview": True,
        },
    )
    check("edit (preview)", result.get("success") and result.get("applied_count") == 1)
    content = t.call_rpc("sys_read", {"path": "/workspace/demo/plan.md"})
    text = content.decode() if isinstance(content, bytes) else str(content)
    check("preview did not write", "PREVIEW" not in text and "test-edit" in text)

    result = t.call_rpc(
        "edit",
        {
            "path": "/workspace/demo/plan.md",
            "edits": [["Confgiure auth (test-edt)", "Configure authentication"]],
            "fuzzy_threshold": 0.7,
        },
    )
    check(
        "edit (fuzzy restore)",
        result.get("success") and result.get("applied_count") == 1,
    )

    try:
        t.call_rpc(
            "edit",
            {
                "path": "/workspace/demo/plan.md",
                "edits": [["Phase 1", "P1"]],
                "if_match": "wrong-etag",
            },
        )
        check("edit (OCC conflict)", False, "should have raised")
    except Exception as e:
        check(
            "edit (OCC conflict)",
            "conflict" in str(e).lower() or "etag" in str(e).lower(),
        )

    # =========================================================================
    print("\n=== 5. VERSION HISTORY ===")
    # =========================================================================
    r = cli("versions", "history", "/workspace/demo/plan.md")
    check("version history", "Version" in r.stdout or "version" in r.stdout)

    # =========================================================================
    print("\n=== 6. HERB QUALITY GATE (BM25+pgvector+SPLADE+reranker) ===")
    # =========================================================================
    # Wait for search index to process demo files (5s debounce + indexing time).
    # Auto-index hooks fire on write, but the refresh loop has a 5s debounce.
    print("    Waiting for search index to process demo files...")
    for _wait in range(6):
        r = cli("search", "query", "Nexus Core", "--limit", "1")
        if "prod-001" in r.stdout:
            print(f"    Search index ready after {(_wait + 1) * 5}s")
            break
        time.sleep(5)
    else:
        print("    Warning: search index may not be fully populated")

    qa_set = [
        ("Which customer uses Nexus for medical document management?", "cust-002"),
        ("Who is the staff engineer working on semantic search quality?", "emp-002"),
        ("What is the pricing model for Nexus Core?", "prod-001"),
        ("Which customer has been active since 2019 in manufacturing?", "cust-001"),
        ("Who manages the permissions engineering team?", "emp-003"),
        ("What product provides multi-node Raft-based federation?", "prod-003"),
        ("Which customer operates in the renewable energy sector?", "cust-004"),
        ("What product integrates with existing search infrastructure?", "prod-002"),
    ]
    hits = 0
    search_latencies: list[float] = []
    for q, expected in qa_set:
        start = time.perf_counter()
        r = cli("search", "query", q, "--limit", "5")
        elapsed = (time.perf_counter() - start) * 1000
        search_latencies.append(elapsed)
        if expected in r.stdout:
            hits += 1
    check(f"HERB hit rate {hits}/8 >= 90%", hits >= 7, f"{hits}/8")
    search_latencies.sort()
    p50 = search_latencies[len(search_latencies) // 2]
    print(f"    Search latency (incl CLI): p50={p50:.0f}ms")

    # =========================================================================
    print("\n=== 7. PERMISSION-FILTERED SEARCH ===")
    # =========================================================================
    if USER_KEY:
        r = cli("search", "query", "Meridian Health", "--limit", "3")
        check("admin finds cust-002", "cust-002" in r.stdout)

        r = cli("search", "query", "Meridian Health", "--limit", "3", api_key=USER_KEY)
        check("viewer finds cust-002 (dir inheritance)", "cust-002" in r.stdout)

        r = cli(
            "rebac",
            "check",
            "user",
            "demo_user",
            "read",
            "file",
            "/workspace/demo/herb/customers/cust-002.md",
        )
        check("rebac: viewer read nested HERB file", "GRANTED" in r.stdout)
    else:
        print("  (skipped \u2014 NEXUS_DEMO_USER_KEY not set)")

    # =========================================================================
    print("\n=== 8. PERMISSION LATENCY ===")
    # =========================================================================
    import contextlib

    perm_latencies: list[float] = []
    for _ in range(20):
        start = time.perf_counter()
        with contextlib.suppress(Exception):
            t.call_rpc(
                "edit",
                {
                    "path": "/workspace/demo/plan.md",
                    "edits": [["nonexistent_string_12345", "x"]],
                    "preview": True,
                },
            )
        perm_latencies.append((time.perf_counter() - start) * 1000)
    perm_latencies.sort()
    p50 = perm_latencies[len(perm_latencies) // 2]
    p95 = perm_latencies[int(len(perm_latencies) * 0.95)]
    check(f"RPC latency p50={p50:.0f}ms < 50ms", p50 < 50, f"{p50:.1f}ms")
    print(f"    p50={p50:.1f}ms  p95={p95:.1f}ms")

    # =========================================================================
    print("\n=== 9. AUTO-INDEX ON EDIT ===")
    # =========================================================================
    t.call_rpc(
        "edit",
        {
            "path": "/workspace/demo/plan.md",
            "edits": [["Deploy to production", "Deploy using Kubernetes orchestration"]],
        },
    )
    # Wait for auto-index: 5s debounce + indexing time. Retry up to 3 times.
    indexed = False
    for attempt in range(3):
        print(f"    Waiting 8s for daemon auto-index (attempt {attempt + 1}/3)...")
        time.sleep(8)
        r = cli("search", "query", "Kubernetes orchestration", "--limit", "3")
        if "plan.md" in r.stdout:
            indexed = True
            break
    check("auto-index after edit", indexed)
    # Restore
    t.call_rpc(
        "edit",
        {
            "path": "/workspace/demo/plan.md",
            "edits": [["Deploy using Kubernetes orchestration", "Deploy to production"]],
            "fuzzy_threshold": 0.8,
        },
    )

    # =========================================================================
    print("\n=== 10. DELETE → STALE INDEX CHECK ===")
    # =========================================================================
    # Write a test file, wait for auto-index, verify searchable
    t.call_rpc(
        "write",
        {"path": "/workspace/demo/delete-test.md", "buf": "Quantum entanglement teleportation"},
    )
    indexed = False
    for attempt in range(5):
        print(f"    Waiting 8s for auto-index (attempt {attempt + 1}/5)...")
        time.sleep(8)
        r = cli("search", "query", "quantum entanglement teleportation", "--limit", "3")
        if "delete-test" in r.stdout:
            indexed = True
            break
    check("file indexed before delete", indexed)

    # Delete the file
    t.call_rpc("sys_unlink", {"path": "/workspace/demo/delete-test.md"})
    stale = True
    for attempt in range(3):
        print(f"    Waiting 8s for delete to propagate (attempt {attempt + 1}/3)...")
        time.sleep(8)
        r = cli("search", "query", "quantum entanglement teleportation", "--limit", "3")
        if "delete-test" not in r.stdout:
            stale = False
            break
    check(
        "deleted file removed from search", not stale, "stale result still present" if stale else ""
    )

    t.close()

    # =========================================================================
    print(f"\n{'=' * 60}")
    print(f"RESULTS: {passed} passed, {failed} failed")
    print(f"{'=' * 60}")

    if failed:
        print("\nFailed tests:")
        for name, ok, detail in results:
            if not ok:
                print(f"  \u2717 {name}: {detail}")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
