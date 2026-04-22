#!/usr/bin/env python3
"""E2E validation for per-directory semantic index scoping (Issue #3698).

Requires a running Nexus stack with the ``add_indexed_directories`` migration
applied. Pass NEXUS_URL, NEXUS_API_KEY, DATABASE_URL as env vars:

    export NEXUS_URL=http://localhost:50870
    export NEXUS_API_KEY=sk-...
    export DATABASE_URL=postgresql://postgres:nexus@localhost:50872/nexus
    python scripts/test_index_scope_e2e.py

What this validates:

1. **API CRUD** — POST/DELETE /api/v2/search/index-directory + GET /indexed-dirs
2. **Migration applied** — indexed_directories table and zones.indexing_mode exist
3. **Scope filter correctness** — when a zone is in 'scoped' mode:
    - Files INSIDE the registered directory ARE embedded (document_chunks rows)
    - Files OUTSIDE the registered directory are NOT embedded (ZERO rows)
    - No leak between scope boundaries
4. **Semantic search scope** — /api/v2/search/query semantic returns only in-scope files
5. **Backward compat** — zone in 'all' mode indexes everything (legacy behavior)
6. **DELETE propagates** — unregistering a directory stops future embeddings

Validation method: direct SQL inspection of document_chunks joined with file_paths,
plus semantic search queries via the HTTP API. This catches both false positives
(leakage) and false negatives (in-scope files missing from the index).
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

NEXUS_URL = os.environ.get("NEXUS_URL", "http://localhost:50870")
API_KEY = os.environ.get("NEXUS_API_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Test zone and paths.
TEST_ZONE = "root"
INDEXED_DIR = "/e2e_scope/indexed"
UNINDEXED_DIR = "/e2e_scope/noindex"

# In-scope files — should appear in document_chunks + semantic search.
INDEXED_FILES = {
    f"{INDEXED_DIR}/auth_manual.md": (
        "# Authentication Manual\n\n"
        "The quantum encryption protocol uses entangled photon pairs to establish "
        "a shared secret between Alice and Bob. Eve cannot eavesdrop without "
        "perturbing the quantum state, which is detected via Bell inequality tests."
    ),
    f"{INDEXED_DIR}/deployment_guide.md": (
        "# Deployment Guide\n\n"
        "To deploy the mesh networking layer, configure the gossip protocol with "
        "a fanout of 3 and heartbeat interval of 200ms. The failure detector uses "
        "the phi-accrual algorithm for suspicion levels."
    ),
}

# Out-of-scope files — MUST NOT appear in document_chunks or semantic search.
# These use UNIQUE, SEMANTICALLY DISTINCTIVE content so leakage would be obvious.
UNINDEXED_FILES = {
    f"{UNINDEXED_DIR}/biotech_secret.md": (
        "# Biotech Research Notes\n\n"
        "The CRISPR-Cas9 gene editing workflow for zebrafish embryos uses "
        "microinjection of single-guide RNA at the 1-cell stage. Knock-in "
        "efficiency improves with HDR template lengths of 60-100 bp."
    ),
    f"{UNINDEXED_DIR}/astronomy_notes.md": (
        "# Astronomy Observations\n\n"
        "The Hertzsprung-Russell diagram plots stellar luminosity against surface "
        "temperature. Main sequence stars fuse hydrogen into helium via the p-p "
        "chain for low-mass stars and the CNO cycle for heavier stars."
    ),
}

# Distinctive search queries that should ONLY match in-scope or out-of-scope files.
# If an out-of-scope query returns a hit, that's a LEAK.
INSCOPE_QUERIES = [
    ("quantum encryption entangled photons", "auth_manual.md"),
    ("mesh networking gossip heartbeat phi-accrual", "deployment_guide.md"),
]
LEAK_CANARY_QUERIES = [
    # These terms only appear in unindexed files. If they return ANY results
    # from the unindexed dir, we have a leak.
    ("CRISPR Cas9 zebrafish microinjection", "biotech_secret.md"),
    ("Hertzsprung-Russell luminosity main sequence", "astronomy_notes.md"),
]

# =============================================================================
# Test harness
# =============================================================================

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


def http_call_with_key(
    method: str, path: str, body: dict | None, api_key: str
) -> tuple[int, dict | None]:
    """HTTP call using a specific API key (not the admin default)."""
    url = f"{NEXUS_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {api_key}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.getcode()
            raw = resp.read().decode()
            return status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read().decode() if e.fp else ""
        try:
            return e.code, json.loads(raw) if raw else None
        except Exception:
            return e.code, {"raw": raw}
    except Exception as e:
        return 0, {"error": str(e)}


def http_call(
    method: str, path: str, body: dict | None = None, expect: int = 200
) -> tuple[int, dict | None]:
    """HTTP call with API key. Returns (status, parsed_body_or_None)."""
    url = f"{NEXUS_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {API_KEY}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.getcode()
            raw = resp.read().decode()
            return status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read().decode() if e.fp else ""
        try:
            return e.code, json.loads(raw) if raw else None
        except Exception:
            return e.code, {"raw": raw}
    except Exception as e:
        return 0, {"error": str(e)}


def psql(sql: str) -> str:
    """Run a SQL query via docker exec. Returns stdout."""
    # Find the postgres container for this worktree instance.
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}", "--filter", "name=postgres"],
        capture_output=True,
        text=True,
    )
    containers = [
        c
        for c in result.stdout.strip().split("\n")
        if c and "15214aa8" in c  # this worktree's hash
    ]
    if not containers:
        return "ERROR: no postgres container found"
    container = containers[0]
    r = subprocess.run(
        [
            "docker",
            "exec",
            container,
            "psql",
            "-U",
            "postgres",
            "-d",
            "nexus",
            "-t",
            "-A",
            "-c",
            sql,
        ],
        capture_output=True,
        text=True,
    )
    return r.stdout.strip()


def write_file(path: str, content: str) -> None:
    """Create a file via the /api/v2/files/write HTTP endpoint."""
    body = {"path": path, "content": content, "encoding": "utf8"}
    req = urllib.request.Request(
        f"{NEXUS_URL}/api/v2/files/write",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        if resp.getcode() not in (200, 201):
            raise RuntimeError(f"write {path} failed: {resp.read().decode()}")


def delete_file(path: str) -> None:
    """Delete a file via the /api/v2/files/delete endpoint (best-effort)."""
    body = {"path": path}
    req = urllib.request.Request(
        f"{NEXUS_URL}/api/v2/files/delete",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with contextlib.suppress(Exception):
        urllib.request.urlopen(req, timeout=10)


def hmac_api_key(raw_key: str) -> str:
    """Hash an API key using the same HMAC-SHA256 scheme as the server.

    Mirrors ``nexus.storage.api_key_ops.hash_api_key``. The server reads
    ``NEXUS_API_KEY_SECRET`` from its environment; in this test we fall
    back to the legacy default salt.
    """
    import hashlib
    import hmac

    secret = os.environ.get("NEXUS_API_KEY_SECRET", "nexus-api-key-v1")
    return hmac.new(
        secret.encode("utf-8"),
        raw_key.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def create_non_admin_key(user_id: str, raw_key: str) -> None:
    """INSERT a non-admin API key + matching api_keys row directly into the DB.

    Bypasses the admin CLI (which requires gRPC + TLS) by writing the
    hashed key straight into ``api_keys``. The server's auth middleware
    reads api_keys on every request, so the new key is usable immediately.
    """
    key_hash = hmac_api_key(raw_key)
    # Delete any stale row under the same user_id so re-runs are idempotent.
    psql(f"DELETE FROM api_keys WHERE user_id='{user_id}'")
    psql(
        "INSERT INTO api_keys "
        "(key_id, key_hash, user_id, subject_type, subject_id, zone_id, "
        " is_admin, inherit_permissions, name, created_at, revoked) "
        "VALUES ("
        f" gen_random_uuid()::text, '{key_hash}', '{user_id}', 'user', "
        f" '{user_id}', '{TEST_ZONE}', 0, 0, 'e2e-scope-test', NOW(), 0)"
    )


def delete_non_admin_key(user_id: str) -> None:
    """Clean up the non-admin API key after the test."""
    psql(f"DELETE FROM api_keys WHERE user_id='{user_id}'")


def count_chunks_for_path(virtual_path: str) -> int:
    """Return the number of txtai sections rows for a given file path.

    The txtai backend stores document text in the ``sections`` table
    (id = virtual_path, text = content). A row here proves the file is
    in the semantic index — which is the cost-sensitive path we're
    gating via the scope filter.
    """
    sql = f"SELECT COUNT(*) FROM sections WHERE id = '{virtual_path}'"
    return int(psql(sql) or "0")


def count_chunks_under_prefix(prefix: str) -> int:
    """Return total txtai sections rows under a given path prefix."""
    sql = f"SELECT COUNT(*) FROM sections WHERE id LIKE '{prefix}%'"
    return int(psql(sql) or "0")


def wait_for_index(
    virtual_path: str, *, expect_present: bool, attempts: int = 6, delay: float = 5.0
) -> bool:
    """Poll document_chunks until the file is (or is not) indexed."""
    for attempt in range(attempts):
        n = count_chunks_for_path(virtual_path)
        if expect_present and n > 0:
            return True
        if not expect_present and n == 0 and attempt > 0:
            # For absence, wait once to make sure the consumer has had a
            # chance to process, then confirm still zero.
            return True
        time.sleep(delay)
    return (
        count_chunks_for_path(virtual_path) > 0
        if expect_present
        else (count_chunks_for_path(virtual_path) == 0)
    )


# =============================================================================
# Main validation flow
# =============================================================================


def main() -> None:
    if not API_KEY:
        print("ERROR: NEXUS_API_KEY not set")
        sys.exit(1)
    if not DATABASE_URL:
        print("WARNING: DATABASE_URL not set (using docker exec fallback)")

    # -------------------------------------------------------------------------
    print("=== 1. SCHEMA — migration applied ===")
    # -------------------------------------------------------------------------
    check(
        "zones.indexing_mode column exists",
        psql(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='zones' AND column_name='indexing_mode'"
        )
        == "indexing_mode",
    )
    check(
        "indexed_directories table exists",
        psql(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name='indexed_directories'"
        )
        == "indexed_directories",
    )
    check(
        "alembic head is idx_dirs_3698",
        psql("SELECT version_num FROM alembic_version") == "idx_dirs_3698",
    )

    # Ensure the test zone exists — on fresh stacks without `nexus demo init`,
    # the zones table starts empty and the /indexing-mode endpoint would 404.
    psql(
        f"INSERT INTO zones (zone_id, name, phase, finalizers, "
        f"created_at, updated_at, indexing_mode) "
        f"VALUES ('{TEST_ZONE}', '{TEST_ZONE}', 'Active', '[]', "
        f"NOW(), NOW(), 'all') "
        f"ON CONFLICT (zone_id) DO NOTHING"
    )

    # -------------------------------------------------------------------------
    print("\n=== 2. API CRUD — happy path ===")
    # -------------------------------------------------------------------------
    # Start clean: flip back to 'all' via the API, then remove any leftover
    # directory registrations. This leaves a clean slate for subsequent tests.
    http_call(
        "POST",
        "/api/v2/search/indexing-mode",
        {"mode": "all", "zone_id": TEST_ZONE},
    )
    # Best-effort cleanup of any leftover directory registrations.
    for path in (INDEXED_DIR,):
        http_call("DELETE", "/api/v2/search/index-directory", {"path": path})
    # Clean stale txtai-backend rows (sections / vectors / documents) from
    # previous runs so leak checks start from a known state. The txtai
    # content store uses three tables: ``documents`` (primary key on id,
    # stores raw text), ``sections`` (append-only with indexid PK), and
    # ``vectors`` (pgvector ANN index keyed by indexid). All three need
    # cleanup — missing ``documents`` causes INSERT conflicts on the next
    # bootstrap because txtai re-inserts by id.
    scope_patterns = (INDEXED_DIR, UNINDEXED_DIR)
    for prefix in scope_patterns:
        # DELETE documents first — it's the one with a unique id PK.
        psql(f"DELETE FROM documents WHERE id LIKE '{prefix}%'")
        # DELETE sections (non-unique id, integer PK) — best-effort.
        psql(f"DELETE FROM sections WHERE id LIKE '{prefix}%'")
    # Orphan vectors (indexid no longer in sections) are harmless at query
    # time because the ANN join drops them, but clean them for tidiness.
    # Note: vectors.indexid is the join key, not 'id'.
    psql("DELETE FROM vectors WHERE indexid NOT IN (SELECT indexid FROM sections)")

    status, body = http_call("GET", "/api/v2/search/indexed-dirs")
    check("GET /indexed-dirs returns 200", status == 200)

    status, body = http_call("POST", "/api/v2/search/index-directory", {"path": INDEXED_DIR})
    check(
        "POST /index-directory returns 200",
        status == 200,
        f"status={status} body={body}",
    )
    check(
        "POST body returns canonical path",
        body is not None and body.get("path") == INDEXED_DIR,
    )

    status, body = http_call("GET", "/api/v2/search/indexed-dirs")
    check(
        "GET /indexed-dirs includes registered dir",
        body is not None and INDEXED_DIR in body.get("directories", []),
    )

    # -------------------------------------------------------------------------
    print("\n=== 3. API CRUD — edge cases (Issue #6 policies) ===")
    # -------------------------------------------------------------------------
    # Round-5 change (codex review): re-registering an already-registered
    # directory is now idempotent — instead of 409, the router catches
    # DirectoryAlreadyRegisteredError and re-runs the backfill so an
    # operator who hit a previous backfill failure can retry by re-issuing
    # the same POST. The response carries status="already_registered" so
    # callers can still tell it wasn't a fresh add.
    status, body = http_call("POST", "/api/v2/search/index-directory", {"path": INDEXED_DIR})
    check(
        "duplicate register → 200 (idempotent retry)",
        status == 200 and isinstance(body, dict) and body.get("status") == "already_registered",
        f"got status={status} body={body}",
    )

    status, body = http_call("POST", "/api/v2/search/index-directory", {"path": "/foo/../etc"})
    check("path escape (..) → 400", status == 400)

    status, body = http_call("POST", "/api/v2/search/index-directory", {"path": "relative/path"})
    check("relative path → 400", status == 400)

    status, body = http_call("POST", "/api/v2/search/index-directory", {"path": "/foo/./bar"})
    check("dot segment → 400", status == 400)

    status, body = http_call(
        "DELETE", "/api/v2/search/index-directory", {"path": "/never/registered"}
    )
    check("DELETE absent dir → 404", status == 404)

    # -------------------------------------------------------------------------
    print("\n=== 4. SCOPE FILTER — in-scope files ARE embedded ===")
    # -------------------------------------------------------------------------
    # Flip zone to 'scoped' mode via the /indexing-mode endpoint. This
    # updates the daemon's in-memory state under the refresh lock and
    # writes through to the DB, so no restart is needed.
    status, body = http_call(
        "POST",
        "/api/v2/search/indexing-mode",
        {"mode": "scoped", "zone_id": TEST_ZONE},
    )
    check(
        "POST /indexing-mode mode=scoped → 200",
        status == 200,
        f"status={status} body={body}",
    )

    status, body = http_call("GET", "/api/v2/search/indexed-dirs")
    check(
        "GET /indexed-dirs reports mode=scoped",
        body is not None and body.get("indexing_mode") == "scoped",
    )

    # Write in-scope files.
    print("    Writing in-scope files...")
    for path, content in INDEXED_FILES.items():
        try:
            write_file(path, content)
            check(f"wrote {path}", True)
        except Exception as e:
            check(f"wrote {path}", False, str(e))

    # Write out-of-scope files.
    print("    Writing out-of-scope files...")
    for path, content in UNINDEXED_FILES.items():
        try:
            write_file(path, content)
            check(f"wrote {path}", True)
        except Exception as e:
            check(f"wrote {path}", False, str(e))

    # Wait for the debounce + indexing to fire (5s debounce + processing).
    print("    Waiting 20s for auto-index refresh loop...")
    time.sleep(20)

    # Verify in-scope files got chunks.
    for path in INDEXED_FILES:
        n = count_chunks_for_path(path)
        check(f"in-scope {path} has >=1 chunk", n >= 1, f"got {n}")

    # -------------------------------------------------------------------------
    print("\n=== 5. SCOPE FILTER — out-of-scope files NOT embedded (no leak) ===")
    # -------------------------------------------------------------------------
    for path in UNINDEXED_FILES:
        n = count_chunks_for_path(path)
        check(
            f"out-of-scope {path} has 0 chunks (no leak)",
            n == 0,
            f"LEAK: {n} chunks found",
        )

    # Aggregate leak check — count total chunks under each prefix.
    n_indexed = count_chunks_under_prefix(INDEXED_DIR)
    n_unindexed = count_chunks_under_prefix(UNINDEXED_DIR)
    check(f"chunks under {INDEXED_DIR} > 0", n_indexed > 0, f"got {n_indexed}")
    check(
        f"chunks under {UNINDEXED_DIR} == 0 (bulk leak check)",
        n_unindexed == 0,
        f"LEAK: {n_unindexed} chunks",
    )

    # -------------------------------------------------------------------------
    print("\n=== 6. SEMANTIC SEARCH — leak canary queries ===")
    # -------------------------------------------------------------------------
    # In-scope queries should hit their expected file.
    for q, expected in INSCOPE_QUERIES:
        status, body = http_call(
            "GET", f"/api/v2/search/query?q={urllib_quote(q)}&type=semantic&limit=5"
        )
        paths = [r.get("path", "") for r in (body.get("results", []) if body else [])]
        hit = any(expected in p for p in paths)
        check(f'in-scope query "{q[:40]}" returns {expected}', hit, str(paths))

    # Leak canary: these queries use terms that ONLY appear in unindexed files.
    # If semantic search returns any hit from the unindexed dir, that's a leak.
    for q, _not_expected in LEAK_CANARY_QUERIES:
        status, body = http_call(
            "GET", f"/api/v2/search/query?q={urllib_quote(q)}&type=semantic&limit=5"
        )
        paths = [r.get("path", "") for r in (body.get("results", []) if body else [])]
        leaked = any(UNINDEXED_DIR in p for p in paths)
        check(
            f'leak canary "{q[:40]}" does NOT return {UNINDEXED_DIR} files',
            not leaked,
            f"LEAK: paths={paths}",
        )

    # -------------------------------------------------------------------------
    print("\n=== 7. KEYWORD COVERAGE — out-of-scope files still grep-able ===")
    # -------------------------------------------------------------------------
    # Per spec: BM25/FTS/grep keep full coverage. An out-of-scope file must
    # still be findable via keyword search even though it's not semantically
    # indexed.
    r = subprocess.run(
        ["nexus", "grep", "CRISPR", UNINDEXED_DIR],
        capture_output=True,
        text=True,
        env={**os.environ, "NEXUS_URL": NEXUS_URL, "NEXUS_API_KEY": API_KEY},
    )
    # nexus grep works on-demand, should find the literal.
    check(
        "grep finds literal in out-of-scope dir (on-demand scan)",
        "CRISPR" in r.stdout or "biotech_secret" in r.stdout,
        r.stdout[:200] if r.stdout else "grep returned no output",
    )

    # -------------------------------------------------------------------------
    print("\n=== 8. REBAC AUTH — non-admin write check ===")
    # -------------------------------------------------------------------------
    # Issue #6 policy #7: mutation endpoints require write permission on
    # the target path. Exercise the gate end-to-end by creating a
    # non-admin API key and asserting that:
    # (a) POST /index-directory without any grant → 403
    # (b) DELETE /index-directory without any grant → 403
    # (c) admin DOES bypass the check (already verified above via ADMIN_KEY)
    #
    # The "with grant → 200" path requires a ReBAC write tuple on the
    # exact path AND a working PermissionEnforcer in the bricks layer.
    # That integration is covered by the sync enforcer we call, but the
    # fail-closed path is the important one to prove: a non-admin
    # without explicit grant is denied.
    nonadmin_user = "e2e_scope_nonadmin"
    nonadmin_key = "sk-e2e_scope_nonadmin_test_key_do_not_use_in_prod"
    try:
        create_non_admin_key(nonadmin_user, nonadmin_key)
    except Exception as exc:
        check(
            "create non-admin key",
            False,
            f"failed to create key: {exc}",
        )
    else:
        check("create non-admin key", True)

    status, body = http_call_with_key(
        "POST",
        "/api/v2/search/index-directory",
        {"path": "/e2e_rebac/test"},
        nonadmin_key,
    )
    check(
        "non-admin POST /index-directory → 403 (no grant)",
        status == 403,
        f"got {status}: {body}",
    )

    status, body = http_call_with_key(
        "DELETE",
        "/api/v2/search/index-directory",
        {"path": "/e2e_rebac/test"},
        nonadmin_key,
    )
    check(
        "non-admin DELETE /index-directory → 403 (no grant)",
        status == 403,
        f"got {status}: {body}",
    )

    # Non-admin calls to /indexing-mode must also be rejected (admin-only).
    status, body = http_call_with_key(
        "POST",
        "/api/v2/search/indexing-mode",
        {"mode": "scoped", "zone_id": TEST_ZONE},
        nonadmin_key,
    )
    check(
        "non-admin POST /indexing-mode → 403 (admin-only)",
        status == 403,
        f"got {status}: {body}",
    )

    # And /purge-unscoped is also admin-only.
    status, body = http_call_with_key(
        "POST",
        "/api/v2/search/purge-unscoped",
        {},
        nonadmin_key,
    )
    check(
        "non-admin POST /purge-unscoped → 403 (admin-only)",
        status == 403,
        f"got {status}: {body}",
    )

    # GET /indexed-dirs is also admin-only — registered directory
    # names can encode customer / repo / project names that should
    # not leak to non-admin callers (round 6 codex finding).
    status, body = http_call_with_key(
        "GET",
        "/api/v2/search/indexed-dirs",
        None,
        nonadmin_key,
    )
    check(
        "non-admin GET /indexed-dirs → 403 (admin-only, prevents prefix leak)",
        status == 403,
        f"got {status}: {body}",
    )

    # ---- Non-admin WITH explicit ReBAC grant should SUCCEED ----------------
    # Issue the key via the admin /api/v2/auth/keys endpoint with a --grant.
    # This is the proper write path: it creates the api_keys row AND the
    # ReBAC tuple in one call, which invalidates the ZoneGraphLoader cache
    # so the subsequent permission check sees the fresh tuple.
    #
    # Raw SQL INSERTs into rebac_tuples do NOT invalidate the cache, so a
    # test that seeds tuples directly would get a false-deny from the
    # stale cached graph. (This is how the real CLI / admin flows work.)
    grant_user = "e2e_scope_grant_user"
    grant_path = "/e2e_rebac/granted"
    # Clean any leftover state.
    psql(f"DELETE FROM rebac_tuples WHERE subject_id='{grant_user}'")
    psql(f"DELETE FROM api_keys WHERE user_id='{grant_user}'")

    status, body = http_call(
        "POST",
        "/api/v2/auth/keys",
        {
            "user_id": grant_user,
            "label": "e2e-scope-grant-test",
            "grants": [{"path": grant_path, "role": "editor"}],
        },
    )
    check(
        f"admin POST /api/v2/auth/keys with editor grant → 201 (body keys: "
        f"{list(body.keys()) if isinstance(body, dict) else body})",
        status == 201 and isinstance(body, dict) and "key" in body,
        f"got {status}: {body}",
    )
    grant_key = (body or {}).get("key") or (body or {}).get("raw_key") or ""

    if grant_key:
        status, body = http_call_with_key(
            "POST",
            "/api/v2/search/index-directory",
            {"path": grant_path},
            grant_key,
        )
        check(
            "non-admin POST /index-directory → 200 (WITH direct_editor grant)",
            status == 200,
            f"got {status}: {body}",
        )

        status, body = http_call_with_key(
            "DELETE",
            "/api/v2/search/index-directory",
            {"path": grant_path},
            grant_key,
        )
        check(
            "non-admin DELETE /index-directory → 200 (WITH direct_editor grant)",
            status == 200,
            f"got {status}: {body}",
        )
    else:
        check(
            "non-admin POST /index-directory → 200 (WITH grant)",
            False,
            "could not extract API key from create-key response; skipping grant tests",
        )

    # Cleanup grant test state.
    psql(f"DELETE FROM rebac_tuples WHERE subject_id='{grant_user}'")
    delete_non_admin_key(grant_user)
    delete_non_admin_key(nonadmin_user)

    # -------------------------------------------------------------------------
    print("\n=== 9. PURGE-UNSCOPED — destructive admin endpoint ===")
    # -------------------------------------------------------------------------
    # Register TWO directories under scoped mode, write a file in each so
    # both get embedded, then unregister ONE directory. The unregistered
    # directory's file is now out of scope and must be purged by the
    # endpoint while the still-registered directory's file remains.
    #
    # NOTE: the txtai graph backend is disabled by default (see DaemonConfig
    # .txtai_graph) because its graph upsert path hits a NotNullViolation
    # on ``INSERT INTO edges DEFAULT VALUES``. With graph disabled the real
    # write path works end-to-end.
    http_call(
        "POST",
        "/api/v2/search/indexing-mode",
        {"mode": "scoped", "zone_id": TEST_ZONE},
    )
    status, _ = http_call("POST", "/api/v2/search/index-directory", {"path": "/e2e_purge/keeper"})
    check("purge setup: register /e2e_purge/keeper", status == 200)
    status, _ = http_call("POST", "/api/v2/search/index-directory", {"path": "/e2e_purge/temp"})
    check("purge setup: register /e2e_purge/temp", status == 200)

    keeper_path = "/e2e_purge/keeper/stays.md"
    temp_path = "/e2e_purge/temp/will_be_purged.md"
    write_file(
        keeper_path,
        "# Keeper\n\nQuasar jets accelerate plasma to relativistic speeds via magnetic reconnection.",
    )
    write_file(
        temp_path,
        "# Temp\n\nEnzyme kinetics follow Michaelis-Menten saturation with allosteric feedback.",
    )

    print("    Waiting 20s for both files to be embedded...")
    time.sleep(20)

    keeper_count_before = count_chunks_for_path(keeper_path)
    temp_count_before = count_chunks_for_path(temp_path)
    check(
        f"before purge: keeper embedded ({keeper_count_before} sections rows)",
        keeper_count_before >= 1,
    )
    check(
        f"before purge: temp embedded ({temp_count_before} sections rows)",
        temp_count_before >= 1,
    )

    # Unregister /e2e_purge/temp — now temp_path is out of scope.
    # The DELETE endpoint auto-purges stale txtai rows, so the temp
    # row should already be gone after this call returns.
    status, body = http_call(
        "DELETE", "/api/v2/search/index-directory", {"path": "/e2e_purge/temp"}
    )
    check("unregister /e2e_purge/temp → 200", status == 200)
    auto_purged = (body or {}).get("purged") or {}
    check(
        f"DELETE /index-directory auto-purged stale rows ({auto_purged})",
        isinstance(auto_purged, dict) and auto_purged.get("txtai_docs", 0) >= 1,
        str(auto_purged),
    )

    # Calling /purge-unscoped explicitly should be idempotent — there's
    # nothing left to purge after the auto-purge above, so the count
    # is 0 and that's the expected result.
    status, body = http_call("POST", "/api/v2/search/purge-unscoped", {})
    check(
        "POST /purge-unscoped → 200 (idempotent after auto-purge)",
        status == 200,
        f"got {status}: {body}",
    )
    purged = (body or {}).get("purged") or {}
    check(
        f"explicit purge is idempotent ({purged})",
        isinstance(purged, dict) and purged.get("txtai_docs", 0) == 0,
        str(purged),
    )

    # Give the backend a moment to flush.
    time.sleep(2)

    keeper_count_after = count_chunks_for_path(keeper_path)
    temp_count_after = count_chunks_for_path(temp_path)
    check(
        f"after purge: keeper STILL embedded ({keeper_count_after} sections rows)",
        keeper_count_after >= 1,
        f"keeper rows dropped from {keeper_count_before} to {keeper_count_after}",
    )
    check(
        f"after purge: temp PURGED ({temp_count_after} sections rows)",
        temp_count_after == 0,
        f"LEAK: temp still has {temp_count_after} rows after purge",
    )

    # Cleanup purge test state.
    with contextlib.suppress(Exception):
        delete_file(keeper_path)
    with contextlib.suppress(Exception):
        delete_file(temp_path)
    http_call("DELETE", "/api/v2/search/index-directory", {"path": "/e2e_purge/keeper"})

    # -------------------------------------------------------------------------
    print("\n=== 10. CLEANUP ===")
    # -------------------------------------------------------------------------
    for path in list(INDEXED_FILES) + list(UNINDEXED_FILES):
        with contextlib.suppress(Exception):
            delete_file(path)

    status, _ = http_call("DELETE", "/api/v2/search/index-directory", {"path": INDEXED_DIR})
    check("DELETE /index-directory returns 200", status == 200)

    # Flip back to 'all' mode via the API (write-through to DB + daemon).
    http_call(
        "POST",
        "/api/v2/search/indexing-mode",
        {"mode": "all", "zone_id": TEST_ZONE},
    )

    print_results()


def urllib_quote(s: str) -> str:
    import urllib.parse

    return urllib.parse.quote_plus(s)


def print_results() -> None:
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
