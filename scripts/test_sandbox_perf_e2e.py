#!/usr/bin/env python3
"""E2E validation for the SANDBOX profile (Issue #3778).

Sibling of ``scripts/test_build_perf_e2e.py``, which exercises the full
multi-container demo stack via CLI+gRPC. SANDBOX is a pip-install,
single-process profile with no Docker-Compose stack, no gRPC server,
no remote CLI surface — so this script drives the *same 10 sections*
through the in-process SDK (``nexus.connect(profile="sandbox")``) and
``nx.service(...)`` calls.

Sections:
    1.  Infra + status (boot, enabled bricks)
    2.  File CRUD (write, sys_read)
    3.  Grep (local file scan)
    4.  Edit (exact, fuzzy, preview, OCC)
    5.  Version history (skipped — BRICK_VERSIONING not in SANDBOX)
    6.  HERB retrieval-quality gate (vector, hit rate >= 7/8)
    7.  Permission enforcement (ReBAC wiring only)
    8.  In-process RPC latency
    9.  Edit → reindex → search (indexed-path freshness)
    10. Delete → deindex → search (indexed-path invalidation)

Usage::

    # Full run (retrieval-quality sections enabled)
    OPENAI_API_KEY=sk-... python3 scripts/test_sandbox_perf_e2e.py

    # Fast mode (no key): skips 6/9/10
    python3 scripts/test_sandbox_perf_e2e.py

Exits 0 on success, 1 on any failure.

Retrieval-quality sections (6/9/10) require a real embedding API because
SANDBOX's retrieval path is ``SqliteVecBackend`` + ``litellm``. Without
a key those sections cleanly skip — the rest (infra / CRUD / grep / edit
/ latency / permissions) still exercise SANDBOX's default config.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import nexus  # noqa: E402
from nexus.cli.commands.demo_data import HERB_CORPUS, HERB_QA_SET  # noqa: E402
from nexus.contracts.constants import ROOT_ZONE_ID  # noqa: E402

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


async def _seed_herb(nx) -> None:
    """Write each HERB file via the public SDK."""
    for path, body, _desc in HERB_CORPUS:
        nx.write(path, body.encode("utf-8"))


async def _svc_grep(
    search_svc, pattern: str, path: str = "/workspace/demo", max_results: int = 20
) -> list[dict]:
    """Grep through SearchService — NexusFS.grep raises NotImplementedError
    for the base class; the service-level entry point is what SANDBOX
    callers use.
    """
    svc = search_svc._service_instance
    return await svc.grep(pattern, path=path, max_results=max_results)


def _resolve_vec_backend(search_svc):
    """Return the wired ``SqliteVecBackend`` instance, or ``None``.

    Used *only* for setup: SANDBOX ships no background auto-indexer, so
    tests must explicitly surface content into the vec backend after
    write / edit / unlink. Retrieval assertions still go through the
    public ``semantic_search`` entrypoint — see ``_public_semantic_hits``.

    Returns None when connect() was not called with
    ``enable_vector_search=True`` or the optional deps (sqlite-vec,
    litellm) are missing.
    """
    svc = search_svc._service_instance
    return getattr(svc, "_sqlite_vec_backend", None)


async def _vec_upsert_file(vec_backend, nx, path: str) -> int:
    """Read a file from SANDBOX and upsert its text into the vec backend.

    Returns the number of rows written. Callers assert ``>= 1``.
    """
    body = nx.sys_read(path).decode("utf-8", errors="replace")
    return await vec_backend.upsert(
        [{"path": path, "chunk_index": 0, "text": body}],
        zone_id=ROOT_ZONE_ID,
    )


def _count_vec_rows(vec_backend, path: str, chunk_index: int = 0) -> int:
    """Count rows in the sqlite-vec table for ``(path, chunk_index, zone)``.

    Read-only verification that ``upsert``'s delete-before-insert
    semantics left exactly one row per stable key — a count of 2 would
    indicate either a rowid collision or a write race. Goes behind the
    ``_conn`` / ``_VEC_TABLE`` private surface because these are
    implementation invariants with no public read path, and using
    ``semantic_search`` for this would conflate row-count with
    embedding similarity.
    """
    from nexus.bricks.search.sqlite_vec_backend import _VEC_TABLE

    conn = vec_backend._conn
    if conn is None:
        return -1
    row = conn.execute(
        f"SELECT COUNT(*) FROM {_VEC_TABLE} WHERE path = ? AND chunk_index = ? AND zone_id = ?",
        (path, chunk_index, ROOT_ZONE_ID),
    ).fetchone()
    return int(row[0]) if row else 0


async def _public_semantic_hits(
    search_svc, query: str, path: str = "/workspace/demo", limit: int = 5
) -> list[dict]:
    """Retrieval via the public ``SearchService.semantic_search`` path.

    On SANDBOX this routes through ``_semantic_search_sandbox`` →
    local sqlite-vec → federation → BM25S. Returns the full hit dicts
    (including ``semantic_degraded`` on fallback) so callers can
    distinguish a real semantic match from a BM25S fallback — the
    retrieval-quality gate must fail when the vector path silently
    degrades.
    """
    svc = search_svc._service_instance
    return await svc.semantic_search(query, path=path, limit=limit, search_mode="semantic")


def _hits_degraded(hits: list[dict]) -> bool:
    """True iff any hit carries ``semantic_degraded=True`` (BM25 fallback)."""
    return any(bool(h.get("semantic_degraded")) for h in hits)


def _paths(hits: list[dict]) -> list[str]:
    return [h.get("path", "") for h in hits]


async def main() -> int:
    global passed, failed

    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    if has_openai:
        print("    [mode] OPENAI_API_KEY present — retrieval-quality sections enabled")
    else:
        print("    [mode] no OPENAI_API_KEY — sections 6/9/10 will skip")

    with tempfile.TemporaryDirectory(prefix="sandbox-e2e-") as tmp:
        # =================================================================
        print("\n=== 1. INFRA & STATUS ===")
        # =================================================================
        t0 = time.monotonic()
        cfg: dict = {
            "profile": "sandbox",
            "data_dir": str(Path(tmp) / "nexus"),
        }
        if has_openai:
            cfg["enable_vector_search"] = True
        nx = await nexus.connect(config=cfg)
        boot_ms = (time.monotonic() - t0) * 1000
        print(f"    Boot time: {boot_ms:.0f} ms")
        check("SANDBOX boots", nx is not None)

        # SANDBOX ships: eventlog, namespace, permissions, cache, ipc,
        # scheduler, agent_runtime, search, mcp, parsers (see
        # _SANDBOX_BRICKS in deployment_profile.py). versioning / llm /
        # pay / observability are OUT.
        search_svc = nx.service("search") if hasattr(nx, "service") else None
        rebac_svc = nx.service("rebac") if hasattr(nx, "service") else None
        check("search service available", search_svc is not None)
        check("rebac service available", rebac_svc is not None)

        try:
            # =============================================================
            print("\n=== 2. FILE CRUD ===")
            # =============================================================
            nx.write("/workspace/demo/README.md", b"# Nexus Demo Workspace\n")
            check(
                "write + sys_read round-trip",
                nx.sys_read("/workspace/demo/README.md") == b"# Nexus Demo Workspace\n",
            )

            nx.write(
                "/workspace/demo/plan.md",
                b"# Plan\n\nPhase 1: Configure authentication.\nPhase 2: Deploy to production.\n",
            )
            check(
                "cat plan.md",
                b"Configure authentication" in nx.sys_read("/workspace/demo/plan.md"),
            )

            # Seed HERB corpus for sections 3, 6, 9, 10.
            await _seed_herb(nx)
            check(
                "HERB corpus seeded (11 files)",
                nx.sys_read("/workspace/demo/herb/customers/cust-002.md").startswith(
                    b"# Customer: Meridian Health"
                ),
            )

            # =============================================================
            print("\n=== 3. GREP / KEYWORD SEARCH ===")
            # =============================================================
            # SANDBOX has no auto-index daemon — tests trigger indexing
            # explicitly. grep() itself reads files directly, no prior
            # indexing needed; but the HERB / auto-index sections will
            # reindex after mutating writes.
            try:
                hits = await _svc_grep(search_svc, "authentication")
                check(
                    "grep 'authentication' finds plan.md",
                    any("plan.md" in (h.get("path") or h.get("file") or "") for h in hits),
                    f"got {len(hits)} hits",
                )
            except Exception as e:
                check("grep 'authentication'", False, f"{type(e).__name__}: {e}")

            try:
                hits = await _svc_grep(search_svc, "HIPAA")
                check(
                    "grep 'HIPAA' finds cust-002.md",
                    any("cust-002" in (h.get("path") or h.get("file") or "") for h in hits),
                    f"got {len(hits)} hits",
                )
            except Exception as e:
                check("grep 'HIPAA'", False, f"{type(e).__name__}: {e}")

            # =============================================================
            print("\n=== 4. EDIT (exact, fuzzy, preview, OCC) ===")
            # =============================================================
            # Exact edit
            r = nx.edit(
                "/workspace/demo/plan.md",
                [("Configure authentication", "Configure auth (test-edit)")],
            )
            check(
                "edit (exact)",
                bool(r.get("success")) and r.get("applied_count") == 1,
                str(r.get("errors") or r),
            )

            # Preview — shouldn't modify the file
            r = nx.edit(
                "/workspace/demo/plan.md",
                [("Configure auth (test-edit)", "PREVIEW")],
                preview=True,
            )
            check(
                "edit (preview)",
                bool(r.get("success")) and r.get("applied_count") == 1,
                str(r.get("errors") or r),
            )
            body = nx.sys_read("/workspace/demo/plan.md").decode("utf-8")
            check(
                "preview did not persist",
                "PREVIEW" not in body and "test-edit" in body,
            )

            # Fuzzy match restore
            r = nx.edit(
                "/workspace/demo/plan.md",
                [("Confgiure auth (test-edt)", "Configure authentication")],
                fuzzy_threshold=0.7,
            )
            check(
                "edit (fuzzy restore)",
                bool(r.get("success")) and r.get("applied_count") == 1,
                str(r.get("errors") or r),
            )

            # OCC conflict — wrong etag must raise or report error
            occ_ok = False
            try:
                r = nx.edit(
                    "/workspace/demo/plan.md",
                    [("Phase 1", "P1")],
                    if_match="wrong-etag",
                )
                # Some implementations return error dict rather than raising.
                if not r.get("success") or "conflict" in str(r.get("errors") or "").lower():
                    occ_ok = True
            except Exception as e:
                occ_ok = "conflict" in str(e).lower() or "etag" in str(e).lower()
            check("edit (OCC conflict)", occ_ok)

            # =============================================================
            print("\n=== 5. VERSION HISTORY ===")
            # =============================================================
            # SANDBOX does not enable BRICK_VERSIONING (see _SANDBOX_BRICKS
            # in contracts/deployment_profile.py). Version history is a
            # FULL-profile feature. Record as intentionally skipped.
            print("    (skipped — BRICK_VERSIONING not enabled in SANDBOX)")

            # =============================================================
            print("\n=== 6. HERB RETRIEVAL-QUALITY GATE (vector) ===")
            # =============================================================
            # Retrieval quality on SANDBOX is asserted via the **public**
            # SearchService.semantic_search entrypoint — the same path MCP
            # and HTTP clients hit. The local SqliteVecBackend is used
            # only as the ingest surface (SANDBOX ships no auto-indexer).
            # Queries are the HERB natural-language questions so ranking
            # / query-understanding regressions would be caught.
            vec_backend = _resolve_vec_backend(search_svc)

            # Fail-closed invariant: if the user opted into retrieval mode
            # by setting OPENAI_API_KEY, the backend MUST be wired. A
            # silent skip here would hide factory-wiring regressions.
            if has_openai:
                check(
                    "vector backend wired when OPENAI_API_KEY set",
                    vec_backend is not None,
                    "enable_vector_search=True did not produce a backend — "
                    "check factory/_wired.py + extras install (sqlite-vec, litellm)",
                )

            if vec_backend is None:
                print(
                    "    (skipped — OPENAI_API_KEY not set; SANDBOX ships no "
                    "keyword-index daemon out-of-box)"
                )
            else:
                # Populate the vector backend from HERB files. One chunk per
                # file — HERB records are short enough that this matches the
                # structure SANDBOX produces when a user calls upsert once
                # per document.
                docs = []
                for p, body, _desc in HERB_CORPUS:
                    docs.append({"path": p, "chunk_index": 0, "text": body})
                t_ing = time.perf_counter()
                n_written = await vec_backend.upsert(docs, zone_id=ROOT_ZONE_ID)
                check(
                    f"HERB ingest wrote {n_written}/{len(docs)} rows",
                    n_written == len(docs),
                    f"expected {len(docs)}, got {n_written}",
                )
                print(
                    f"    Ingested {n_written} HERB files "
                    f"({(time.perf_counter() - t_ing) * 1000:.0f} ms)"
                )

                hits_count = 0
                degraded_count = 0
                per_q: list[tuple[str, bool, list[str], bool]] = []
                for qa in HERB_QA_SET:
                    q = qa["question"]
                    expected = qa["expected_file"]
                    try:
                        hits = await _public_semantic_hits(search_svc, q, limit=5)
                    except Exception as e:
                        per_q.append((q, False, [f"error: {e}"], False))
                        continue
                    paths = _paths(hits)
                    degraded = _hits_degraded(hits)
                    if degraded:
                        degraded_count += 1
                    if expected in paths and not degraded:
                        # Count as a hit ONLY when the vector path served
                        # the query — a BM25 fallback hit is a silent
                        # degradation, not a retrieval-quality win.
                        hits_count += 1
                        per_q.append((q, True, paths, degraded))
                    else:
                        per_q.append((q, False, paths, degraded))
                for q, ok, paths, degraded in per_q:
                    mark = "\u2713" if ok else "\u2717"
                    deg_note = " [degraded→BM25]" if degraded else ""
                    print(f"    {mark} {q[:56]}{deg_note}")
                    if not ok:
                        print(f"        top-5: {paths}")
                check(
                    "no semantic_degraded results in vector mode",
                    degraded_count == 0,
                    f"{degraded_count}/8 queries fell back to BM25 — vector path is broken",
                )
                check(
                    f"HERB retrieval {hits_count}/8 >= 7/8 (non-degraded)",
                    hits_count >= 7,
                    f"{hits_count}/8 ({hits_count * 100 // 8}%)",
                )

            # =============================================================
            print("\n=== 7. PERMISSION ENFORCEMENT ===")
            # =============================================================
            # SANDBOX is single-tenant by design (see Issue #3778 spec Q8).
            # ReBAC multi-user scenarios are out-of-scope at this tier —
            # the service is wired so the surface exists, but the script
            # only checks presence, not cross-user behaviour.
            check("rebac service wired", rebac_svc is not None)

            # =============================================================
            print("\n=== 8. IN-PROCESS RPC LATENCY ===")
            # =============================================================
            # SANDBOX has no RPC layer — we measure the equivalent: a
            # round-trip through the service registry (edit-preview is the
            # closest match to the E2E test's "edit" RPC).
            latencies: list[float] = []
            for _ in range(20):
                start = time.perf_counter()
                nx.edit(
                    "/workspace/demo/plan.md",
                    [("nonexistent_string_12345", "x")],
                    preview=True,
                )
                latencies.append((time.perf_counter() - start) * 1000)
            latencies.sort()
            p50 = latencies[len(latencies) // 2]
            p95 = latencies[int(len(latencies) * 0.95)]
            check(f"edit-preview p50={p50:.1f}ms < 50ms", p50 < 50.0, f"{p50:.1f}ms")
            print(f"    p50={p50:.1f}ms  p95={p95:.1f}ms")

            # =============================================================
            print("\n=== 9. EDIT → REINDEX → INDEXED SEARCH ===")
            # =============================================================
            # Exercise the real index-freshness invariant via the public
            # semantic_search path:
            #   1. Edit plan.md (verify the edit returned success).
            #   2. Re-upsert into the vec backend (verify rows written).
            #   3. Assert: NEW query returns plan.md (freshness).
            #   4. Assert: OLD query does NOT return plan.md first
            #      (stale-vector invalidation — duplicate / stale row
            #      regressions would make this fail).
            if vec_backend is None:
                print("    (skipped — OPENAI_API_KEY not set)")
            else:
                edit_res = nx.edit(
                    "/workspace/demo/plan.md",
                    [("Deploy to production", "Deploy using Kubernetes orchestration")],
                )
                check(
                    "edit mutation succeeded",
                    bool(edit_res.get("success")) and edit_res.get("applied_count") == 1,
                    str(edit_res.get("errors") or edit_res),
                )
                rows = await _vec_upsert_file(vec_backend, nx, "/workspace/demo/plan.md")
                check("re-upsert wrote >= 1 row", rows >= 1, f"rows={rows}")

                fresh_hits = await _public_semantic_hits(
                    search_svc, "Kubernetes orchestration", limit=5
                )
                check(
                    "freshness query not degraded (vector path served)",
                    not _hits_degraded(fresh_hits),
                    "semantic_degraded=True — fell back to BM25",
                )
                new_paths = _paths(fresh_hits)
                check(
                    "freshness — new content is top-K via semantic_search",
                    "/workspace/demo/plan.md" in new_paths,
                    f"top-5: {new_paths}",
                )
                # Mutation-consistency invariant: upsert must leave
                # exactly one row for (path, chunk_index, zone_id). Two
                # rows would mean the delete-before-insert pattern or
                # the stable_rowid contract broke — and semantic-
                # similarity queries can't distinguish that from
                # ranking variance (OpenAI embeddings semantically match
                # plan.md for "Deploy to production" even after the
                # edit).
                row_count = _count_vec_rows(vec_backend, "/workspace/demo/plan.md")
                check(
                    "exactly one vec row per (path, chunk_index) after upsert",
                    row_count == 1,
                    f"got {row_count} rows",
                )

                # Restore + reindex for idempotence
                nx.edit(
                    "/workspace/demo/plan.md",
                    [
                        (
                            "Deploy using Kubernetes orchestration",
                            "Deploy to production",
                        )
                    ],
                    fuzzy_threshold=0.8,
                )
                await _vec_upsert_file(vec_backend, nx, "/workspace/demo/plan.md")

            # =============================================================
            print("\n=== 10. DELETE → DEINDEX → INDEXED SEARCH ===")
            # =============================================================
            # Deletion half of the index-invalidation contract:
            #   1. Write + upsert + verify file is searchable (public path).
            #   2. Unlink + vec_backend.delete — assert rows_deleted >= 1
            #      so a silent no-op delete regression would fail.
            #   3. Assert absent from a WIDER window (limit=20) — catches
            #      the case where ranking variance pushes stale content
            #      out of top-5 but it's still indexed.
            if vec_backend is None:
                print("    (skipped — OPENAI_API_KEY not set)")
            else:
                del_path = "/workspace/demo/delete-test.md"
                nx.write(del_path, b"Quantum entanglement teleportation\n")
                upserted = await _vec_upsert_file(vec_backend, nx, del_path)
                check("pre-delete upsert wrote row", upserted >= 1, f"rows={upserted}")

                pre_hits = await _public_semantic_hits(search_svc, "Quantum entanglement", limit=5)
                check(
                    "pre-delete query not degraded (vector path served)",
                    not _hits_degraded(pre_hits),
                    "semantic_degraded=True — fell back to BM25",
                )
                paths = _paths(pre_hits)
                check(
                    "pre-delete file surfaces via semantic_search",
                    del_path in paths,
                    f"top-5: {paths}",
                )

                nx.sys_unlink(del_path)
                deleted_rows = await vec_backend.delete([del_path], zone_id=ROOT_ZONE_ID)
                check(
                    f"vec_backend.delete rows >= 1 (actual={deleted_rows})",
                    deleted_rows >= 1,
                    "silent no-op delete",
                )
                # Direct row-count check — authoritative for "is the
                # deletion persisted"; semantic_search alone would
                # conflate this with ranking noise.
                post_count = _count_vec_rows(vec_backend, del_path)
                check(
                    "post-delete vec row count == 0",
                    post_count == 0,
                    f"got {post_count} rows still present",
                )

                # Wider window via the public path — catches stale rows
                # that a top-5 ranking quirk could otherwise hide.
                wide_hits = await _public_semantic_hits(
                    search_svc, "Quantum entanglement", limit=20
                )
                wide_paths = _paths(wide_hits)
                check(
                    "post-delete deleted path absent from top-20",
                    del_path not in wide_paths,
                    f"stale entry still indexed: {wide_paths}",
                )

        finally:
            nx.close()

    # =====================================================================
    print(f"\n{'=' * 60}")
    print(f"RESULTS: {passed} passed, {failed} failed")
    print(f"{'=' * 60}")

    if failed:
        print("\nFailed tests:")
        for name, ok, detail in results:
            if not ok:
                print(f"  \u2717 {name}: {detail}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
