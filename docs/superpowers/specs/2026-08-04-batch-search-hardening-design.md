# Batch search hardening — restore `/query/batch` to its documented contract

**Date:** 2026-08-04
**Status:** Approved design
**Driver:** DeepBuildAI/koodle#2050 (adopt batch search for koodle's cross-workspace
fan-out — deferred until this lands; koodle currently fires 3 path-scoped
queries × N workspaces of single `/query` calls under a 7s deadline).

## Problem

`POST /api/v2/search/query/batch` promises (its own docstring): "embeds all
query texts in ONE OpenAI API call, then runs each through the full hybrid
pipeline". Since #3699 removed the txtai backend, the implementation no longer
delivers any of that:

1. **Sequential execution.** `_batch_search_on_current_loop` awaits each inner
   `self.search(...)` in a `for` loop. A batch of N hybrid queries costs
   N × single-query latency — strictly worse than N parallel `/query` calls.
2. **No embedding amortisation.** Each inner search embeds its own query text.
   A batch of 24 queries sharing one text (the koodle fan-out shape: same
   query, 24 path scopes) pays 24 embedding API calls.
3. **Tuning params silently dropped.** Only `search_type`/`limit`/`path_filter`
   are forwarded into `SearchRequest`. `alpha`, `fusion_method`, `rrf_k`,
   `expand`, `recency*` are ignored — a caller using the measured
   weighted/α0.3 hybrid tuning (+20% MRR on koodle prod data) loses it on the
   batch path with no error.
4. **Response-shape drift.** Batch formats hits with its own inline dict and
   omits `chunk_index`, `line_start`/`line_end`, `splade_score`,
   `reranker_score`, `semantic_degraded`, `macro_*`, `recency_boost` — all
   present on single `/query` via `_serialize_search_result`.
5. **Failures indistinguishable from empty.** Inner exceptions are swallowed
   into `hits = []`. A caller with a fail-closed coverage contract (koodle's
   `searched/failed/partial`) cannot tell "no matches" from "backend fell
   over".

## Goals

- Batch of N queries ≈ wall-clock of the slowest inner query (bounded
  concurrency), not the sum.
- One `embed_batch` call per batch covering unique query texts.
- Full single-`/query` param parity (public names + legacy aliases).
- Byte-parity hit serialization with single `/query`.
- Per-query error surfacing; batch-level failures unchanged.
- Purely additive response changes; existing callers keep working.

## Non-goals

- Federated / cross-zone batch (endpoint stays single-zone by design, #4557).
- `graph_mode` in batch.
- Koodle-side adoption (tracked in koodle#2050, explicitly deferred).
- Query-count cap (existing 470-query benchmark usage; revisit if abused).
- Prod deploy/repoint (rides the next routine release train).

## API contract

### Request

Each entry in `queries` mirrors single-`/query` public parameter names.
Legacy batch aliases stay accepted:

```json
{
  "queries": [
    {
      "q": "quarterly revenue",            // alias: "query"
      "type": "hybrid",                    // alias: "search_type"; keyword|semantic|hybrid
      "limit": 10,                          // 1..100; violations → per-query error (single-query parity)
      "path": "/workspaces/x/documents/",  // alias: "path_filter"
      "alpha": 0.3,                         // 0.0..1.0
      "fusion": "weighted",                // alias: "fusion_method"; rrf|weighted|rrf_weighted
      "rrf_k": 60,                          // 1..1000
      "expand": "none",                    // none|macro
      "recency": "auto",                   // off|on|auto
      "recency_weight": 0.5,               // 0.0..5.0
      "recency_half_life_days": 30         // >0..3650
    }
  ]
}
```

Public name wins when both a public name and its alias are present.
Out-of-range values are a **per-query error entry**, not a batch-level 400 —
batch-level 400 stays reserved for a missing/empty/non-list `queries` array.
`adaptive_k` is not exposed (internal knob, not on the single route either).

### Response

Top level unchanged: `queries`, `total_queries`, `latency_ms`,
`avg_per_query_ms`, `permission_filter_ms`. Per-entry:

- `results` serialized via the shared `_serialize_search_result` — entries
  gain `chunk_index`, `line_start`, `line_end`, `splade_score`,
  `reranker_score`, and the conditional `title_score` / `context` /
  `tier_boost` / `recency_boost` / `semantic_degraded` / `macro_*` fields.
  Exact single-query parity, including its rounding and None semantics.
- A failed query yields `{"query": ..., "results": [], "total": 0,
  "error": "<message>"}`. `error` is additive and present **only** on
  failure — absence of `error` means the result set is genuine.
- Batch-level failures unchanged: 401/403 (auth/read gate), 503 (daemon
  initializing), 400 (no queries).

ReBAC policy unchanged; the over-fetch switches from a hardcoded ×3 to the
same `_compute_rebac_fetch_limit` helper single `/query` uses.

## Design

### Router (`src/nexus/server/api/v2/routers/search.py` + new helper)

`search.py` sits at ~1750 lines against the 2000-line file cap, so batch spec
parsing/validation and response shaping move to a sibling helper module
`_search_batch.py` (same pattern as `_search_serialize.py`):

- `parse_batch_query_specs(raw) -> list[ParsedSpec | SpecError]` — alias
  resolution, type/range validation mirroring the single route's Query
  bounds, defaults.
- Response assembly: per-entry serialization via `_serialize_search_result`,
  error-entry shaping, ReBAC trim (unchanged policy).

The route keeps: auth, #4557 read gate, daemon-init check, permission
enforcer resolution, top-level timing fields.

### Daemon (`_batch_search_on_current_loop`)

1. **Pre-embed once.** Collect unique query texts among valid specs whose
   effective type ≠ `keyword`, when `self._embedding_client` is present.
   One `embed_batch(unique_texts)` call → `text → vector` map. Fail-soft:
   on exception, log and proceed with an empty map (inner searches embed
   individually — exactly today's degradation, where hybrid falls back to
   keyword-only if embedding fails).
2. **`query_vector` rides `SearchRequest`.** New optional field
   `query_vector: list[float] | None = None` on the frozen dataclass
   (`contracts/search_types.py`) — the dataclass was introduced precisely so
   new knobs are pure data additions. `search()` forwards it;
   `_search_on_current_loop` uses it in the two legs that currently call
   `self._embed_query(query)` (semantic + hybrid):
   `qvec = request-provided vector if set, else await _embed_query(query)`.
   `embed_ms` timing reads 0 for pre-embedded queries.
3. **Bounded concurrency.** Replace the sequential loop with
   `asyncio.gather(*tasks, return_exceptions=True)` where each task runs
   under an `asyncio.Semaphore` of size
   `NEXUS_SEARCH_BATCH_CONCURRENCY` (new `SearchConfig` field via
   `get_env_int`, default 8). Concurrent inner searches are the normal prod
   condition already (concurrent `/query` HTTP requests); `_run_on_owner_loop`
   has a same-loop shortcut, so gathering on the owner loop cannot deadlock.
4. **Per-query failure isolation.** A raised inner exception becomes that
   entry's `error`; other entries unaffected. The path-context attach pass
   (#3773) skips error entries.

Return contract of the daemon method becomes
`list[list[SearchResult] | BatchQueryFailure]` (a tiny dataclass carrying the
message), preserving 1:1 positional mapping with the request.

### Net effect for the koodle fan-out shape (deferred adopter)

24 queries (8 workspaces × 3 sub-paths, one query text): 1 HTTP request,
1 embedding call, 8-wide server-side parallelism, full weighted/α0.3 tuning,
chunk-level fidelity, and per-query failures that map cleanly onto koodle's
fail-closed `searched/failed/partial` coverage contract.

## Testing

- **Daemon unit** (`tests/unit/bricks/search/`): embed-once (mock embedding
  client; 24 same-text hybrid queries → exactly 1 `embed_batch` call);
  `query_vector` set → `_embed_query` not called; keyword-only batch → no
  embedding at all; per-query exception isolation (one poisoned query, others
  succeed); semaphore bound respected (peak concurrent inner searches ≤
  configured); embed-batch failure degrades to per-query embedding.
- **Router** (mock daemon, existing pattern): alias + public-name parsing
  (public wins); tuning params reach `SearchRequest` (alpha/fusion/rrf_k/
  expand/recency trio); serializer parity (batch entry == single-query
  serialization for the same `SearchResult`); per-query error entries for
  invalid specs and daemon-reported failures; limit range check; ReBAC over-fetch
  via `_compute_rebac_fetch_limit`; #4557 read-gate tests stay green
  unchanged.
- **E2E** (`tests/e2e/self_contained/test_search_surface_live_e2e.py`):
  extend the existing batch block — mixed-type batch (keyword + semantic +
  hybrid), assert hit-shape parity against a single `/query` for the same
  query, assert `error` absent on healthy entries.
- **Bench evidence for the PR:** local instance, 24 × same-text hybrid batch:
  wall-clock + embedding-call count, before vs after.

## Compatibility & rollback

- Response changes additive; request aliases preserved — existing callers
  (`scripts/test_read_gate_e2e.py`, benchmark harnesses) unaffected.
- No schema/alembic delta → plain-image rollback stays valid.
- New env `NEXUS_SEARCH_BATCH_CONCURRENCY` optional (default 8). Setting it
  to 1 restores sequential execution (ops fallback) while keeping the other
  fixes.

## References

- koodle#2050 (driver; koodle-side adoption deferred there)
- #3699 (txtai removal that degraded batch to a sequential loop)
- #4557 (batch read gate — policy preserved)
- #4544 (batch/single parity precedent for `tier_boost`)
- #4541/#4551/#4553 (the tuning/recency params batch must forward)
