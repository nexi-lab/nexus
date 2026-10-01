# Federated search: Python -> Rust port scoping

Status: **LANDED** (originally scoped 2026-09-13; ported across PRs #4790-#4818
and merged 2026-09-24).  Code SSOT lives at
[`nexus-vfs/rust/federated-search/`](https://github.com/nexi-lab/nexus-vfs/tree/main/rust/federated-search)
(relocated from `nexus/rust/services/federated-search/` in nexus-vfs#319 +
nexus#4825); the shared peer-fanout primitives live at
[`nexus-vfs/rust/search-common/`](https://github.com/nexi-lab/nexus-vfs/tree/main/rust/search-common).
This document is preserved as design history; consult the code for current
shape.  Author: elfenlieds7 · Superset of the "Federated dispatcher port to
axum handler group" row in
[`nexus-search-architecture.html`](nexus-search-architecture.html#open).

The last major R10 chunk after PRs #4693 (grep/query axum), #4700 (auth
middleware), #4721-#4735 (Rust ReBAC), #4756 (auth-keys), #4760-#4764
(SANDBOX chain delete).  Aims to remove the final Python-owned request path
on `/api/v2/search/query` so `nexusd-cluster --features full` can serve
multi-zone search end-to-end without the FastAPI layer in the loop.

## What ports

The Python `_handle_federated_search` in
[`src/nexus/server/api/v2/routers/search.py`](../../src/nexus/server/api/v2/routers/search.py)
lines 722-910 (~190 LoC) fans out cross-zone search through
[`FederatedSearchDispatcher`](../../src/nexus/bricks/search/federated_search.py)
(1192 LoC).  The dispatcher's dependency closure:

| File | LoC | Role |
| --- | ---:| --- |
| `bricks/search/federated_search.py` | 1192 | Dispatcher class, zone cache, per-leg run, RRF fuse, remote-zone gRPC call |
| `bricks/search/fusion.py` | 470 | `rrf_multi_fusion` + weighted fusion algorithms |
| `bricks/search/daemon.py` | 577 | Daemon abstraction + `daemon_pooling_cap` |
| `bricks/search/result_builders.py` | 185 | `cap_chunks_per_page` + result shaping |
| `bricks/search/results.py` | 197 | Result dataclasses + `BACKEND_LEG_TIMING_KEYS` |
| `bricks/search/search_degraded.py` | 69 | `is_all_peers_failed`, `_zone_results_degraded` |
| `bricks/search/zone_registry.py` | ~200 | `ZoneSearchRegistry` — per-zone daemon lookup + capability lookup |

Total closure ≈ 2900 LoC of Python to reason about, plus the 190 LoC HTTP
wrapper.

## What survives, what disappears

The Rust plugin already does **same-zone, cross-plugin-instance** fanout
inside its own tantivy+HNSW layer.  Federated search here is
**cross-zone**: subject has grants to N zones, each zone has its own
daemon endpoint, results must be fused with a single ranking scale after a
per-zone auth intersection.

Cross-zone ReBAC intersection stays essential: a subject with `owner` on
zone A and `viewer` on zone B may not see the same rows in each even if
the query text is identical.

## New / extended interfaces the Rust side needs

1. **`nexus-rebac` API extension** — `list_accessible_zones(subject: Subject) -> Vec<ZoneId>`.
   Currently the crate only exposes tuple-level lookups and
   `list_zones` on the ZoneManager (federation membership, not
   per-subject grants).  The Python dispatcher hits
   `self._rebac.list_accessible_zones(subject=subject)` on every
   federated request (with a per-subject TTL cache).  This is the
   single biggest new Rust surface.
2. **`SearchDelegation` credential** — short-lived cross-zone auth
   token.  Python `contracts/search_delegation.py` shape must exist
   in Rust; PR-2 below covers minting + wire encoding.
3. **`ZoneSearchRegistry` (Rust)** — per-zone daemon target lookup +
   optional capability advertisement.  Simple map today (single
   plugin, no per-zone routing in production); can start as a
   `HashMap<ZoneId, PluginEndpoint>` behind a trait.
4. **`FederatedSearchResponse` proto** — the dispatcher's return
   shape (`results`, `zones_searched`, `zones_failed`, `latency_ms`,
   `search_timing`, `semantic_degraded`, `cached`) already has a
   Python dataclass; needs a proto message in
   `rust/services/proto/nexus/search/*.proto` so the axum handler
   and the dispatcher can share a wire format.

## Suggested PR breakdown (5 PRs, each independently landable)

**PR 1 — pure fusion + result types (~500 LoC net add)**
- Port `results.py::BaseSearchResult`, `FederatedSearchResponse`,
  `BACKEND_LEG_TIMING_KEYS`, `ZoneFailure` → new
  `rust/services/search-common/` crate.
- Port `fusion.py::rrf_multi_fusion` + weighted fusion → same crate.
- Port `search_degraded.py::is_all_peers_failed` → same crate.
- No behaviour change on Python side; the Rust crate stays unused
  until PR 3.  Test parity: seed the same inputs to both, assert
  identical rankings + ties.

**PR 2 — SearchDelegation + zone registry (~800 LoC)**
- Rust `SearchDelegation` type + proto message + verifier.
- Minimal `ZoneSearchRegistry` trait + in-memory impl (matches
  today's production shape: one plugin, all zones route to it).
- `nexus-rebac` gets `list_accessible_zones(subject)` with the same
  TTL cache the Python side has (per subject_key, monotonic
  expiry).  This is the largest new surface.
- Verifier: `SearchDelegation::verify_and_decode(token, expected_zone)`.

**PR 3 — Rust dispatcher, single-daemon (~600 LoC)**
- New crate `rust/services/federated-search/` (or module inside
  `http-api`; call it out based on whether other handlers need it).
- Port `FederatedSearchDispatcher::search()` for the
  local-daemon-only case: fan out per-zone queries, collect,
  fuse, stamp `zones_searched` / `zones_failed`.
- No cross-zone gRPC yet — every accessible zone goes to the same
  local plugin.  Matches the "Phase 1" comment in the Python
  dispatcher.
- Tests: use the search-plugin-e2e image with two seeded zones,
  assert cross-zone RRF ordering matches the Python dispatcher on
  the same corpus.

**PR 4 — cross-zone gRPC with SearchDelegation (~400 LoC)**
- Extend the dispatcher with the remote-zone leg: for zones whose
  `registry.get(zone_id)` returns a non-local endpoint, mint a
  `SearchDelegation`, dial the remote plugin's `Search` RPC, fold
  results into the same fusion pass.
- Match Python's per-zone latency accounting so timing breakdowns
  round-trip through the axum handler.
- Tests: cross-machine docker E2E (federation-runbook already has
  the 2-machine harness).

**PR 5 — /v2/search/query federated branch + Python delete (~300 LoC net delete)**
- Axum handler `handlers::search::query` grows a
  "federated (multi-zone token) vs single-zone" fork; multi-zone
  goes through the new dispatcher, single-zone keeps the current
  fast path.
- `_handle_federated_search` in Python router deleted; call site
  in `search_query` returns 501 or delegates to `axum` via
  in-process routing (which pattern we already use for other
  migrated paths).
- SANDBOX BM25S fallback: today's Python fallback lives in
  `SearchService.semantic_search`, which is still Python.  This
  PR keeps the Rust dispatcher's `semantic_degraded=true` stamp
  and lets the Python fallback stay in `SearchService` until the
  full SearchService delete arc.

## Risks / gotchas caught during scoping

- **ReBAC `list_accessible_zones` TTL cache invariant** — the Python
  cache is per-subject monotonic-clock keyed; a subject whose grants
  change between two federated queries within the TTL window keeps
  seeing stale zones.  The Rust port MUST preserve this shape (or
  add an explicit invalidator hooked into the tuple-write path).
  Silently tightening the cache to zero-TTL breaks the p99 story
  the dispatcher exists to defend.
- **`SearchDelegation` clock skew** — the Python minter uses UTC
  wall-clock; Rust should too, and both sides need to tolerate
  ±30 s skew (already documented in `contracts/search_delegation.py`).
- **RRF tie-break** — Python `rrf_multi_fusion` uses insertion
  order to tie-break equal-score docs.  Rust `Vec::sort_by` is
  stable, so as long as the leg-result iteration order matches
  (sorted by zone_id like Python does), rankings will be
  byte-identical.  Verify in PR 1's parity test.
- **Per-file ReBAC filter** — when `enable_per_file_rebac=true`
  (default), the dispatcher calls `filter_federated_results` after
  fusion.  This is a per-hit path check with its own cache; port
  order matters — must run AFTER fusion, BEFORE the caller's
  `limit` cap, so a filtered-out hit doesn't leave a hole in the
  page.
- **SANDBOX degradation** — the Python router adds a fallback that
  calls `SearchService._semantic_with_sandbox_fallback` when all
  peers fail; that helper is still Python and needs
  `SearchService` alive.  PR 5 must leave a call-through for the
  degraded case rather than deleting it, or plan the fallback's
  Rust port as part of the SearchService delete arc.
- **`request.app.state` couplings** — the Python router pulls
  `rebac_service`, `zone_search_registry`, `federated_per_file_rebac`,
  `deployment_profile`, `nexus_fs` from FastAPI app state.  In Rust
  these become fields on the axum `State<AppState>` — already wired
  for other handlers, so an additive extension not a redesign.

## Approx effort

- PR 1: ~1 day (mostly typing + parity tests)
- PR 2: ~2 days (SearchDelegation crypto surface + rebac API design review)
- PR 3: ~2 days
- PR 4: ~2-3 days (cross-machine E2E under `federation-runbook.html`)
- PR 5: ~1-2 days
- Total ~8-10 days elapsed, executable in parallel with other kernel work.

## Follow-on: full `SearchService` delete

After PR 5, `src/nexus/bricks/search/search_service.py` (~4400 LoC)
still owns the SQL fallback, the daemon shim, the ReBAC post-filter
and the SANDBOX BM25S degraded path.  Everything except the SANDBOX
fallback is behind Rust-served handlers now; a follow-on arc can
delete SearchService entirely once the SANDBOX degraded path either
moves to Rust or is dropped (the Rust plugin's own BM25S is a
plausible substitute; verify recall parity first).
