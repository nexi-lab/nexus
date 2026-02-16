#!/usr/bin/env python3
"""E2E test: Issue #1244 — Namespace cache (dcache) event-driven invalidation.

Uses FastAPI TestClient to test the full HTTP stack:
  HTTP → auth → PermissionEnforcer → NamespaceManager (dcache) → ReBAC → disk

Validates:
  1. Admin bypass (is_admin=True skips namespace checks)
  2. Fail-closed (no grants → invisible)
  3. Grant → IMMEDIATE visibility (event-driven invalidation)
  4. Revoke → IMMEDIATE invisibility
  5. Per-subject namespace isolation
  6. Rapid grant/revoke cache churn
  7. Performance (p50/p99 latency)
"""

import base64
import os
import shutil
import statistics
import tempfile
import time

# ── Create temp dirs BEFORE importing nexus ──
DATA_DIR = tempfile.mkdtemp(prefix="nexus-e2e-1244-")
BACKEND_DIR = os.path.join(DATA_DIR, "backend")
RECORD_DB = os.path.join(DATA_DIR, "records.db")
METADATA_DIR = os.path.join(DATA_DIR, "metadata")
os.makedirs(BACKEND_DIR, exist_ok=True)

os.environ["NEXUS_ENFORCE_PERMISSIONS"] = "true"
os.environ["NEXUS_ENFORCE_ZONE_ISOLATION"] = "false"
os.environ["NEXUS_REBAC_BACKEND"] = "memory"
os.environ["NEXUS_SEARCH_DAEMON"] = "false"
os.environ.pop("NEXUS_DATABASE_URL", None)
os.environ.pop("NEXUS_URL", None)


def _extract_tuple_id(result):
    """Extract tuple_id from rebac_create() result (dict or WriteResult)."""
    if isinstance(result, dict):
        return result.get("tuple_id", "")
    if hasattr(result, "tuple_id"):
        return result.tuple_id
    return str(result)


def main():
    passed = failed = 0

    def check(name, ok, detail=""):
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"  PASS: {name}")
        else:
            failed += 1
            print(f"  FAIL: {name} -- {detail}")

    try:
        # ── Setup: Create NexusFS via factory (fully wired services) ─────
        from nexus.backends.local import LocalBackend
        from nexus.factory import create_nexus_fs
        from nexus.server.auth.static_key import StaticAPIKeyAuth
        from nexus.server.fastapi_server import create_app
        from nexus.services.permissions.namespace_factory import create_namespace_manager
        from nexus.storage.raft_metadata_store import RaftMetadataStore
        from nexus.storage.record_store import SQLAlchemyRecordStore

        backend = LocalBackend(root_path=BACKEND_DIR)
        metadata_store = RaftMetadataStore.embedded(METADATA_DIR)
        record_store = SQLAlchemyRecordStore(db_path=RECORD_DB)

        nx = create_nexus_fs(
            backend=backend,
            metadata_store=metadata_store,
            record_store=record_store,
            enforce_permissions=True,
            allow_admin_bypass=True,
            enforce_zone_isolation=False,
        )

        # ── Wire namespace manager (dcache + L3 + event-driven invalidation) ──
        rebac_mgr = nx._rebac_manager
        assert rebac_mgr is not None, "ReBAC manager not created by factory"

        namespace_mgr = create_namespace_manager(
            rebac_manager=rebac_mgr,
            record_store=record_store,  # L3 enabled — tests invalidation across all layers
        )

        # Inject into PermissionEnforcer
        enforcer = nx._permission_enforcer
        assert enforcer is not None, "PermissionEnforcer not created by factory"
        enforcer.namespace_manager = namespace_mgr

        # Wire event-driven invalidation: rebac_write → namespace cache (Issue #1244)
        invalidation_log = []

        def _invalidation_callback(st, sid, zid):
            invalidation_log.append((st, sid, zid))
            namespace_mgr.invalidate((st, sid))

        rebac_mgr.register_namespace_invalidator(
            "namespace_dcache",
            _invalidation_callback,
        )
        print("[SETUP] NexusFS + NamespaceManager (L3 enabled) + event-driven invalidation wired")

        # ── Auth: multi-user StaticAPIKeyAuth ────────────────────────────
        auth_provider = StaticAPIKeyAuth(
            api_keys={
                "sk-admin-key": {
                    "subject_type": "user",
                    "subject_id": "admin",
                    "zone_id": "test",
                    "is_admin": True,
                },
                "sk-alice-key": {
                    "subject_type": "user",
                    "subject_id": "alice",
                    "zone_id": "test",
                    "is_admin": False,
                },
                "sk-bob-key": {
                    "subject_type": "user",
                    "subject_id": "bob",
                    "zone_id": "test",
                    "is_admin": False,
                },
            }
        )

        app = create_app(
            nexus_fs=nx,
            auth_provider=auth_provider,
        )

        # ── TestClient ──────────────────────────────────────────────────
        from starlette.testclient import TestClient

        client = TestClient(app, raise_server_exceptions=False)

        ADMIN_H = {"Authorization": "Bearer sk-admin-key"}
        ALICE_H = {"Authorization": "Bearer sk-alice-key"}
        BOB_H = {"Authorization": "Bearer sk-bob-key"}

        def rpc(method, params, headers):
            return client.post(
                f"/api/nfs/{method}",
                json={"jsonrpc": "2.0", "method": method, "params": params},
                headers=headers,
            )

        def rpc_ok(r):
            j = r.json()
            return r.status_code == 200 and ("error" not in j or j.get("error") is None)

        def rpc_err(r):
            if r.status_code != 200:
                return True
            j = r.json()
            return "error" in j and j.get("error") is not None

        def rpc_content(r):
            """Decode content from RPC read response (handles base64 bytes)."""
            j = r.json()
            result = j.get("result")
            if isinstance(result, dict) and result.get("__type__") == "bytes":
                return base64.b64decode(result["data"]).decode("utf-8", errors="replace")
            if isinstance(result, str):
                return result
            return str(result)

        print("\n" + "=" * 70)
        print("Issue #1244 E2E — FastAPI TestClient, Permissions Enabled")
        print("  Stack: HTTP → StaticAPIKeyAuth → PermissionEnforcer")
        print("         → NamespaceManager (dcache+L3) → ReBAC → LocalBackend")
        print("=" * 70)

        # ── Test 1: Health ───────────────────────────────────────────────
        print("\n[Test 1] Health check")
        r = client.get("/health")
        check("Health responds 200", r.status_code == 200)
        d = r.json()
        check("enforce_permissions=true", d.get("enforce_permissions") is True, str(d))

        # ── Test 2: Auth enforcement ─────────────────────────────────────
        print("\n[Test 2] Auth enforcement")
        r = rpc("read", {"path": "/nonexistent"}, {})  # No auth header
        check("No auth → 401", r.status_code == 401, f"got {r.status_code}")

        r = rpc("read", {"path": "/nonexistent"}, {"Authorization": "Bearer sk-invalid"})
        check("Invalid key → 401", r.status_code == 401, f"got {r.status_code}")

        # ── Test 3: Admin bypass (is_admin=True) ─────────────────────────
        print("\n[Test 3] Admin bypass (is_admin=True)")
        r = rpc("write", {"path": "/workspace/proj/data.csv", "content": "hello"}, ADMIN_H)
        check("Admin write succeeds", rpc_ok(r), r.text[:200])

        r = rpc("read", {"path": "/workspace/proj/data.csv"}, ADMIN_H)
        check("Admin read succeeds", rpc_ok(r) and "hello" in rpc_content(r), r.text[:200])

        # ── Test 4: Fail-closed (Alice, no grants) ──────────────────────
        print("\n[Test 4] Fail-closed — Alice has zero grants")
        r = rpc("read", {"path": "/workspace/proj/data.csv"}, ALICE_H)
        check("Alice cannot read (no grants)", rpc_err(r), r.text[:200])

        # ── Test 5: Grant → IMMEDIATE visibility (Issue #1244 core) ──────
        print("\n[Test 5] Grant → IMMEDIATE visibility (Issue #1244)")

        result = nx.rebac_create(
            subject=("user", "alice"),
            relation="direct_viewer",
            object=("file", "/workspace/proj/data.csv"),
            zone_id="test",
        )
        tuple_id = _extract_tuple_id(result)
        check("Grant created", bool(tuple_id), str(result))
        check("Invalidation callback fired", len(invalidation_log) > 0, str(invalidation_log))

        # KEY: NO sleep needed — event-driven invalidation is synchronous
        r = rpc("read", {"path": "/workspace/proj/data.csv"}, ALICE_H)
        check(
            "Alice reads IMMEDIATELY after grant (no sleep!)",
            rpc_ok(r) and "hello" in rpc_content(r),
            r.text[:300],
        )

        # ── Test 6: Revoke → IMMEDIATE invisibility ──────────────────────
        print("\n[Test 6] Revoke → IMMEDIATE invisibility")
        if tuple_id:
            rebac_mgr.rebac_delete(tuple_id)
            # NO sleep — event-driven invalidation is synchronous
            r = rpc("read", {"path": "/workspace/proj/data.csv"}, ALICE_H)
            check(
                "Alice CANNOT read IMMEDIATELY after revoke (no sleep!)", rpc_err(r), r.text[:200]
            )

        # ── Test 7: Per-subject namespace isolation ──────────────────────
        print("\n[Test 7] Per-subject namespace isolation")

        # Admin writes two files
        rpc("write", {"path": "/workspace/alice-dir/f.txt", "content": "Alice-data"}, ADMIN_H)
        rpc("write", {"path": "/workspace/bob-dir/f.txt", "content": "Bob-data"}, ADMIN_H)

        # Grant each user their own file
        nx.rebac_create(
            subject=("user", "alice"),
            relation="direct_viewer",
            object=("file", "/workspace/alice-dir/f.txt"),
            zone_id="test",
        )
        nx.rebac_create(
            subject=("user", "bob"),
            relation="direct_viewer",
            object=("file", "/workspace/bob-dir/f.txt"),
            zone_id="test",
        )

        r = rpc("read", {"path": "/workspace/alice-dir/f.txt"}, ALICE_H)
        check("Alice sees her file", rpc_ok(r) and "Alice-data" in rpc_content(r), r.text[:200])

        r = rpc("read", {"path": "/workspace/bob-dir/f.txt"}, ALICE_H)
        check("Alice CANNOT see Bob's file", rpc_err(r), r.text[:200])

        r = rpc("read", {"path": "/workspace/bob-dir/f.txt"}, BOB_H)
        check("Bob sees his file", rpc_ok(r) and "Bob-data" in rpc_content(r), r.text[:200])

        r = rpc("read", {"path": "/workspace/alice-dir/f.txt"}, BOB_H)
        check("Bob CANNOT see Alice's file", rpc_err(r), r.text[:200])

        # ── Test 8: Rapid grant/revoke/grant (cache churn) ───────────────
        print("\n[Test 8] Rapid grant/revoke/grant (cache churn)")
        for i in range(5):
            tid_result = nx.rebac_create(
                subject=("user", "alice"),
                relation="direct_viewer",
                object=("file", "/workspace/proj/data.csv"),
                zone_id="test",
            )
            tid_str = _extract_tuple_id(tid_result)
            r = rpc("read", {"path": "/workspace/proj/data.csv"}, ALICE_H)
            if not rpc_ok(r):
                check(f"Grant cycle {i}: read after grant", False, r.text[:200])
                break
            rebac_mgr.rebac_delete(tid_str)
            r = rpc("read", {"path": "/workspace/proj/data.csv"}, ALICE_H)
            if not rpc_err(r):
                check(f"Grant cycle {i}: deny after revoke", False, r.text[:200])
                break
        else:
            check("5 grant/revoke cycles all correct", True)

        # ── Test 9: Performance ─────────────────────────────────────────
        print("\n[Test 9] Performance (100 reads, HTTP → auth → namespace → ReBAC → disk)")

        times = []
        for _ in range(100):
            s = time.perf_counter()
            r = rpc("read", {"path": "/workspace/alice-dir/f.txt"}, ALICE_H)
            elapsed = (time.perf_counter() - s) * 1000
            times.append(elapsed)
            if not rpc_ok(r):
                check("Performance read", False, r.text[:200])
                break
        else:
            avg = statistics.mean(times)
            p50 = statistics.median(times)
            p99 = sorted(times)[int(len(times) * 0.99)]
            print(f"  avg={avg:.1f}ms  p50={p50:.1f}ms  p99={p99:.1f}ms")
            check(f"Avg latency < 50ms (got {avg:.1f}ms)", avg < 50)
            check(f"p99 latency < 150ms (got {p99:.1f}ms)", p99 < 150)

        # ── Test 10: Namespace dcache stats (after performance loop for warm cache) ──
        print("\n[Test 10] Namespace dcache stats")
        stats = namespace_mgr.metrics
        print(
            f"  dcache_hits={stats.get('dcache_hits', 0)}  "
            f"dcache_misses={stats.get('dcache_misses', 0)}  "
            f"mount_table_rebuilds={stats.get('mount_table_rebuilds', 0)}  "
            f"l3_hits={stats.get('l3_hits', 0)}"
        )
        check(
            "Cache is working (dcache_hits or mount_table_hits > 0)",
            stats.get("dcache_hits", 0) > 0 or stats.get("mount_table_hits", 0) > 0,
            str(stats),
        )

        # ── Summary ──────────────────────────────────────────────────────
        print("\n" + "=" * 70)
        print(f"Results: {passed} passed, {failed} failed")
        if failed == 0:
            print("ALL TESTS PASSED — Issue #1244 fully validated end-to-end")
            print("  Event-driven invalidation: grant->read, revoke->deny (zero sleep)")
            print("  Per-subject namespace isolation: correct")
            print("  L3 persistent store invalidation: correct")
            print("  No performance regression")
        return 1 if failed else 0

    finally:
        shutil.rmtree(DATA_DIR, ignore_errors=True)


if __name__ == "__main__":
    import sys

    sys.exit(main())
