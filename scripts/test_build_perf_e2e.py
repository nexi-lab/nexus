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
_step_num = 0
_section_t0 = time.perf_counter()


def step(description: str) -> None:
    global _step_num
    _step_num += 1
    elapsed = time.perf_counter() - _section_t0
    print(f"  ── step {_step_num}: {description}  [{elapsed:.1f}s]", flush=True)


def section(title: str) -> None:
    global _section_t0, _step_num
    _section_t0 = time.perf_counter()
    _step_num = 0
    print(f"\n{'=' * 60}", flush=True)
    print(f"{title}", flush=True)
    print(f"{'=' * 60}", flush=True)


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✓ {name}", flush=True)
    else:
        failed += 1
        msg = f"  ✗ {name}"
        if detail:
            msg += f" — {detail}"
        print(msg, flush=True)
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
    # Always log the command for step-by-step traceability
    brief_args = " ".join(str(a) for a in args[:6])
    print(f"    → nexus {brief_args}", file=sys.stderr, flush=True)
    t0 = time.perf_counter()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        elapsed = time.perf_counter() - t0
        if r.returncode != 0:
            brief = " ".join(args[:3])
            stderr_lines = (r.stderr or "").strip().splitlines()[:8]
            stdout_lines = (r.stdout or "").strip().splitlines()[:4]
            print(
                f"    [cli: {brief!r} rc={r.returncode} t={elapsed:.1f}s]",
                file=sys.stderr,
                flush=True,
            )
            if stderr_lines:
                print(f"    stderr: {stderr_lines}", file=sys.stderr, flush=True)
            if stdout_lines:
                print(f"    stdout: {stdout_lines}", file=sys.stderr, flush=True)
        else:
            print(
                f"    ✓ rc=0 t={elapsed:.1f}s stdout={len(r.stdout)}B",
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

    grpc_addr = f"localhost:{GRPC_PORT}"
    print(
        f"  gRPC transport: {grpc_addr}  tls={tls_config is not None}", file=sys.stderr, flush=True
    )
    return RPCTransport(grpc_addr, auth_token=ADMIN_KEY, tls_config=tls_config)


def main() -> None:
    if not ADMIN_KEY:
        print("ERROR: NEXUS_API_KEY not set")
        sys.exit(1)

    print(
        f"NEXUS_URL={NEXUS_URL}  GRPC_PORT={GRPC_PORT}  USER_KEY={'set' if USER_KEY else 'unset'}",
        flush=True,
    )

    print("\nConnecting gRPC transport...", flush=True)
    t = rpc_transport()

    # =========================================================================
    section("1. INFRA & STATUS")
    # =========================================================================

    step("health endpoint GET /healthz/ready")
    try:
        resp = urllib.request.urlopen(f"{NEXUS_URL}/healthz/ready", timeout=5)
        health = resp.read().decode()
        print(f"    response: {health[:120]!r}", file=sys.stderr, flush=True)
        check("Health endpoint", "ready" in health)
    except Exception as e:
        print(f"    error: {e}", file=sys.stderr, flush=True)
        check("Health endpoint", False, str(e))

    step("nexus status --json")
    r = cli("status", "--json")
    check(
        "nexus status",
        "server_reachable" in r.stdout or "healthy" in r.stdout.lower(),
        r.stdout[:200] if r.returncode != 0 else "",
    )

    step("nexus ls /workspace/demo")
    r = cli("ls", "/workspace/demo")
    check("Server reachable (ls)", r.returncode == 0 or "data" in r.stdout)
    check(
        "Demo files exist",
        "README.md" in r.stdout,
        f"stdout={r.stdout[:300]!r}" if "README.md" not in r.stdout else "",
    )

    step("nexus ls /workspace/demo/herb/customers")
    r = cli("ls", "/workspace/demo/herb/customers")
    check(
        "HERB corpus (5 customers)",
        "cust-005" in r.stdout,
        f"stdout={r.stdout[:300]!r}" if "cust-005" not in r.stdout else "",
    )

    step("nexus ls /workspace/demo/herb/employees")
    r = cli("ls", "/workspace/demo/herb/employees")
    check(
        "HERB corpus (employees)",
        "emp-003" in r.stdout,
        f"stdout={r.stdout[:300]!r}" if "emp-003" not in r.stdout else "",
    )

    step("nexus ls /workspace/demo/herb/products")
    r = cli("ls", "/workspace/demo/herb/products")
    check(
        "HERB corpus (products)",
        "prod-003" in r.stdout,
        f"stdout={r.stdout[:300]!r}" if "prod-003" not in r.stdout else "",
    )

    # =========================================================================
    section("2. FILE CRUD")
    # =========================================================================

    step("nexus cat /workspace/demo/README.md")
    r = cli("cat", "/workspace/demo/README.md")
    check(
        "cat README.md",
        "Nexus Demo Workspace" in r.stdout,
        f"stdout={r.stdout[:200]!r}" if "Nexus Demo Workspace" not in r.stdout else "",
    )

    step("nexus cat /workspace/demo/auth-flow.md")
    r = cli("cat", "/workspace/demo/auth-flow.md")
    check(
        "cat auth-flow.md",
        "authentication" in r.stdout.lower(),
        f"stdout={r.stdout[:200]!r}" if "authentication" not in r.stdout.lower() else "",
    )

    # =========================================================================
    section("3. GREP / KEYWORD SEARCH")
    # =========================================================================

    step("nexus grep 'authentication' /workspace/demo")
    r = cli("grep", "authentication", "/workspace/demo")
    check(
        "grep 'authentication'",
        "data" in r.stdout,
        f"stdout={r.stdout[:200]!r}" if "data" not in r.stdout else "",
    )

    step("nexus grep 'vector' /workspace/demo")
    r = cli("grep", "vector", "/workspace/demo")
    check(
        "grep 'vector'",
        "data" in r.stdout,
        f"stdout={r.stdout[:200]!r}" if "data" not in r.stdout else "",
    )

    # =========================================================================
    section("4. EDIT (exact, fuzzy, preview, OCC)")
    # =========================================================================

    step("RPC edit exact match")
    result = t.call_rpc(
        "edit",
        {
            "path": "/workspace/demo/plan.md",
            "edits": [["Configure authentication", "Configure auth (test-edit)"]],
        },
    )
    print(f"    result: {result}", file=sys.stderr, flush=True)
    check("edit (exact)", result.get("success") and result.get("applied_count") == 1, str(result))

    step("RPC edit preview (dry-run)")
    result = t.call_rpc(
        "edit",
        {
            "path": "/workspace/demo/plan.md",
            "edits": [["Configure auth (test-edit)", "PREVIEW"]],
            "preview": True,
        },
    )
    print(f"    result: {result}", file=sys.stderr, flush=True)
    check("edit (preview)", result.get("success") and result.get("applied_count") == 1, str(result))

    step("RPC sys_read verifying preview did not write")
    content = t.call_rpc("sys_read", {"path": "/workspace/demo/plan.md"})
    text = content.decode() if isinstance(content, bytes) else str(content)
    check(
        "preview did not write",
        "PREVIEW" not in text and "test-edit" in text,
        f"text contains PREVIEW={('PREVIEW' in text)}, test-edit={('test-edit' in text)}",
    )

    step("RPC edit fuzzy restore")
    result = t.call_rpc(
        "edit",
        {
            "path": "/workspace/demo/plan.md",
            "edits": [["Confgiure auth (test-edt)", "Configure authentication"]],
            "fuzzy_threshold": 0.7,
        },
    )
    print(f"    result: {result}", file=sys.stderr, flush=True)
    check(
        "edit (fuzzy restore)",
        result.get("success") and result.get("applied_count") == 1,
        str(result),
    )

    step("RPC edit OCC conflict (expect exception)")
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
        print(f"    exception (expected): {e}", file=sys.stderr, flush=True)
        check(
            "edit (OCC conflict)",
            "conflict" in str(e).lower() or "etag" in str(e).lower(),
            str(e)[:200],
        )

    # =========================================================================
    section("5. VERSION HISTORY")
    # =========================================================================

    step("nexus versions history /workspace/demo/plan.md")
    r = cli("versions", "history", "/workspace/demo/plan.md")
    check(
        "version history",
        "Version" in r.stdout or "version" in r.stdout,
        f"stdout={r.stdout[:300]!r}" if "ersion" not in r.stdout else "",
    )

    # =========================================================================
    section("6. HERB QUALITY GATE (BM25+pgvector+SPLADE+reranker)")
    # =========================================================================

    step("waiting for search index to process demo files (5s debounce)")
    print("    Waiting for search index to process demo files...", flush=True)
    for _wait in range(6):
        r = cli("search", "query", "Nexus Core", "--limit", "1")
        if "prod-001" in r.stdout:
            print(f"    Search index ready after {(_wait + 1) * 5}s", flush=True)
            break
        print(f"    not ready yet (attempt {_wait + 1}/6), waiting 5s...", flush=True)
        time.sleep(5)
    else:
        print("    Warning: search index may not be fully populated", flush=True)

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
    for i, (q, expected) in enumerate(qa_set, 1):
        step(f"HERB QA {i}/8: expected={expected!r}")
        start = time.perf_counter()
        r = cli("search", "query", q, "--limit", "5")
        elapsed = (time.perf_counter() - start) * 1000
        search_latencies.append(elapsed)
        hit = expected in r.stdout
        if hit:
            hits += 1
            print(f"    hit: {expected} found in results  ({elapsed:.0f}ms)", flush=True)
        else:
            print(f"    miss: {expected} NOT in results  ({elapsed:.0f}ms)", flush=True)
            print(f"    stdout: {r.stdout[:300]!r}", file=sys.stderr, flush=True)
    check(f"HERB hit rate {hits}/8 >= 90%", hits >= 7, f"{hits}/8")
    search_latencies.sort()
    p50 = search_latencies[len(search_latencies) // 2]
    print(f"    Search latency (incl CLI): p50={p50:.0f}ms", flush=True)

    # =========================================================================
    section("7. PERMISSION-FILTERED SEARCH")
    # =========================================================================

    if USER_KEY:
        step("admin search for Meridian Health")
        r = cli("search", "query", "Meridian Health", "--limit", "3")
        check(
            "admin finds cust-002",
            "cust-002" in r.stdout,
            f"stdout={r.stdout[:300]!r}" if "cust-002" not in r.stdout else "",
        )

        step("viewer search for Meridian Health (dir inheritance)")
        r = cli("search", "query", "Meridian Health", "--limit", "3", api_key=USER_KEY)
        check(
            "viewer finds cust-002 (dir inheritance)",
            "cust-002" in r.stdout,
            f"stdout={r.stdout[:300]!r}" if "cust-002" not in r.stdout else "",
        )

        step("rebac check viewer read nested HERB file")
        r = cli(
            "rebac",
            "check",
            "user",
            "demo_user",
            "read",
            "file",
            "/workspace/demo/herb/customers/cust-002.md",
        )
        check(
            "rebac: viewer read nested HERB file",
            "GRANTED" in r.stdout,
            f"stdout={r.stdout[:300]!r}" if "GRANTED" not in r.stdout else "",
        )
    else:
        print("  (skipped — NEXUS_DEMO_USER_KEY not set)", flush=True)

    # =========================================================================
    section("8. PERMISSION LATENCY")
    # =========================================================================

    import contextlib

    step("20× RPC edit (preview, nonexistent string) for latency measurement")
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
    print(f"    p50={p50:.1f}ms  p95={p95:.1f}ms", flush=True)

    # =========================================================================
    section("9. AUTO-INDEX ON EDIT")
    # =========================================================================

    step("RPC edit: write 'Kubernetes orchestration' to plan.md")
    t.call_rpc(
        "edit",
        {
            "path": "/workspace/demo/plan.md",
            "edits": [["Deploy to production", "Deploy using Kubernetes orchestration"]],
        },
    )
    indexed = False
    for attempt in range(3):
        step(f"waiting 8s for daemon auto-index (attempt {attempt + 1}/3)")
        time.sleep(8)
        r = cli("search", "query", "Kubernetes orchestration", "--limit", "3")
        print(f"    stdout: {r.stdout[:200]!r}", file=sys.stderr, flush=True)
        if "plan.md" in r.stdout:
            indexed = True
            break
    check("auto-index after edit", indexed, "plan.md never appeared in search results after 3×8s")

    step("RPC edit: restore original plan.md text")
    t.call_rpc(
        "edit",
        {
            "path": "/workspace/demo/plan.md",
            "edits": [["Deploy using Kubernetes orchestration", "Deploy to production"]],
            "fuzzy_threshold": 0.8,
        },
    )

    # =========================================================================
    section("10. DELETE → STALE INDEX CHECK")
    # =========================================================================

    step("RPC write /workspace/demo/delete-test.md with unique string")
    t.call_rpc(
        "write",
        {"path": "/workspace/demo/delete-test.md", "buf": "Quantum entanglement teleportation"},
    )
    indexed = False
    for attempt in range(5):
        step(f"waiting 8s for auto-index (attempt {attempt + 1}/5)")
        time.sleep(8)
        r = cli("search", "query", "quantum entanglement teleportation", "--limit", "3")
        print(f"    stdout: {r.stdout[:200]!r}", file=sys.stderr, flush=True)
        if "delete-test" in r.stdout:
            indexed = True
            break
    check(
        "file indexed before delete",
        indexed,
        "delete-test.md never appeared in search results after 5×8s",
    )

    step("RPC sys_unlink /workspace/demo/delete-test.md")
    t.call_rpc("sys_unlink", {"path": "/workspace/demo/delete-test.md"})
    stale = True
    for attempt in range(3):
        step(f"waiting 8s for delete to propagate (attempt {attempt + 1}/3)")
        time.sleep(8)
        r = cli("search", "query", "quantum entanglement teleportation", "--limit", "3")
        print(f"    stdout: {r.stdout[:200]!r}", file=sys.stderr, flush=True)
        if "delete-test" not in r.stdout:
            stale = False
            break
    check(
        "deleted file removed from search",
        not stale,
        "stale result still present after 3×8s" if stale else "",
    )

    t.close()

    # =========================================================================
    section("RESULTS")
    # =========================================================================
    print(f"\nRESULTS: {passed} passed, {failed} failed", flush=True)

    if failed:
        print("\nFailed tests:")
        for name, ok, detail in results:
            if not ok:
                print(f"  ✗ {name}: {detail}")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
