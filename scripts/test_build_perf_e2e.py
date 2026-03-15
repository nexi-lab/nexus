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
    env = {**os.environ, "NEXUS_GRPC_PORT": GRPC_PORT}
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def rpc_transport():
    """Create an RPC transport for direct server calls."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    from nexus.remote.rpc_transport import RPCTransport

    return RPCTransport(f"localhost:{GRPC_PORT}", auth_token=ADMIN_KEY)


def main() -> None:
    if not ADMIN_KEY:
        print("ERROR: NEXUS_API_KEY not set")
        sys.exit(1)

    # =========================================================================
    print("=== 1. INFRA ===")
    # =========================================================================
    r = cli("ls", "/workspace/demo")
    check("Server reachable (ls)", r.returncode == 0 or "data" in r.stdout)
    check("Demo files exist", "README.md" in r.stdout)

    r = cli("ls", "/workspace/demo/herb/customers")
    check("HERB corpus (5 customers)", "cust-005" in r.stdout)

    # =========================================================================
    print("\n=== 2. FILE CRUD ===")
    # =========================================================================
    r = cli("cat", "/workspace/demo/README.md")
    check("cat README.md", "Nexus Demo Workspace" in r.stdout)

    # =========================================================================
    print("\n=== 3. EDIT ===")
    # =========================================================================
    t = rpc_transport()

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
    check("edit (fuzzy restore)", result.get("success") and result.get("applied_count") == 1)

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
        check("edit (OCC conflict)", "conflict" in str(e).lower() or "etag" in str(e).lower())

    # =========================================================================
    print("\n=== 4. VERSION HISTORY ===")
    # =========================================================================
    r = cli("versions", "history", "/workspace/demo/plan.md")
    check("version history", "Version" in r.stdout or "version" in r.stdout)

    # =========================================================================
    print("\n=== 5. HERB QUALITY GATE ===")
    # =========================================================================
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
    for q, expected in qa_set:
        r = cli("search", "query", q, "--limit", "5")
        if expected in r.stdout:
            hits += 1
    check(f"HERB hit rate {hits}/8 >= 90%", hits >= 7, f"{hits}/8")

    # =========================================================================
    print("\n=== 6. PERMISSION-FILTERED SEARCH ===")
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
    print("\n=== 7. AUTO-INDEX ON EDIT ===")
    # =========================================================================
    t.call_rpc(
        "edit",
        {
            "path": "/workspace/demo/plan.md",
            "edits": [["Deploy to production", "Deploy using Kubernetes orchestration"]],
        },
    )
    time.sleep(8)
    r = cli("search", "query", "Kubernetes orchestration", "--limit", "3")
    check("auto-index after edit", "plan.md" in r.stdout)
    # Restore
    t.call_rpc(
        "edit",
        {
            "path": "/workspace/demo/plan.md",
            "edits": [["Deploy using Kubernetes orchestration", "Deploy to production"]],
            "fuzzy_threshold": 0.8,
        },
    )

    t.close()

    # =========================================================================
    print(f"\n{'=' * 60}")
    print(f"RESULTS: {passed} passed, {failed} failed")
    print(f"{'=' * 60}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
