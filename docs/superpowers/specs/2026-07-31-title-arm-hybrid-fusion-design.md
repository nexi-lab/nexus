# Title Arm in Hybrid Fusion (Issue #4545) — Design

**Date:** 2026-07-31
**Issue:** [#4545](https://github.com/nexi-lab/nexus/issues/4545) — search: fold the skeleton title index (locate) into hybrid fusion as a title arm
**Status:** Approved

## Problem

Document titles are not searchable text in `document_chunks` — a query that names a
document by its title only matches if the title happens to recur in body chunks.
Title-shaped lookups ("<project> design doc", "Q3 report") under-rank their target.

A title index already exists: `document_skeleton` (BM25-lite over path tokens and
title, no embeddings), mirrored in-memory as `_skeleton_docs` and scored with title
weighted 2× by `SearchDaemon.locate()`. It currently serves only
`POST /api/v2/search/locate`.

## Decision Summary

| Decision | Choice |
|---|---|
| Arm placement | Keyword sub-fusion (3-arm `rrf_multi_fusion`), final #4541 stage untouched |
| Hydration | Borrow best chunk from fetched kw legs; batched `fetch_ranges` for uncovered paths; chunkless docs emit `chunk_text=""` |
| Attribution | New `BaseSearchResult.title_score: float \| None`, serialized omit-when-None |
| Config | `title_arm: bool = True` + `NEXUS_SEARCH_TITLE_ARM` env kill-switch |
| `vector_score` mislabel | Fixed as a side effect (accepted): page-only hits no longer carry a spurious `vector_score` from the kw sub-fusion |

## Architecture

Scope: the `_search_via_backends` hybrid branch only (`daemon.py`). Keyword and
semantic modes, the legacy fallback stack, and remote federation are untouched.

Current hybrid chain:

```
chunk_kw ─┐
          ├─ rrf_fusion (plain RRF, k=rrf_k) ─ kw_fused ─┐
page_kw ──┘                                              ├─ fuse_results (#4541 knobs) ─ fused ─ page pooling
dense ───────────────────────────────────────────────────┘
```

New chain (title arm added to the keyword sub-fusion):

```
chunk_kw ──┐
page_kw ───┼─ rrf_multi_fusion([chunk, page, title], k=rrf_k) ─ kw_fused ─┐
title_hits ┘                                                              ├─ fuse_results (#4541 knobs) ─ fused ─ page pooling
dense ────────────────────────────────────────────────────────────────────┘
```

### Data flow

1. Legs gather unchanged: `chunk_kw`, `page_kw` (PG only), `dense`.
2. **Title arm** (when `config.title_arm` is true):
   `locate(query, zone_id=zone_id, limit=limit * 2, path_prefix=path_filter)` —
   pure in-memory scan, timed under a new `title_ms` timing key that covers
   locate plus hydration.
3. **Hydration** of `{path, score, title}` hits to page granularity:
   - Coverage map path → representative row: prefer the `page_kw` entry (already
     best-of-page), else the best-scored `chunk_kw` entry. Borrowing the leg's
     `chunk_index` aligns the `path:chunk_index` fusion dedup key so RRF votes
     accumulate on the same fused entry instead of splitting.
   - Uncovered paths: one batched
     `self._vector_backend.fetch_ranges([(path, 0, 0), …], zone_id)` call
     (NeighborFetcher protocol, exists on `PgVectorBackend` and
     `SqliteVecBackend` since #4398) → real chunk-0 text and line info.
   - No row returned (chunkless doc): emit `chunk_text=""`, `chunk_index=0` —
     the doc stays retrievable.
   - Output: `title_hits: list[BaseSearchResult]` in locate rank order, with
     `score` = locate score and `zone_id` set.
4. **Sub-fusion swap**:
   `rrf_fusion(chunk_kw, page_kw, k=rrf_k, limit=limit*2, id_key=None)` becomes
   `rrf_multi_fusion([("chunk", chunk_kw), ("page", page_kw), ("title", title_hits)], k=rrf_k, limit=limit*2, id_key=None)`.
   The title arm is listed last so leg dicts win the first-seen base copy
   (richer line/offset fields); `rrf_multi_fusion` sets `{source}_score` on the
   stored dict for every arm vote, so `title_score` lands on merged entries too.
   The same call is used on the SQLite branch (empty `page_kw` arm is a no-op),
   unifying the two branches.
5. Final stage and pooling unchanged: `fuse_results(kw_fused, dense, config=…)`
   keeps #4541 `alpha` / `fusion_method` / `rrf_k` semantics exactly; the
   `title_score` dict key survives both `fuse_results` and
   `_aggregate_chunks_to_pages` (both pass dicts through).

### Component changes

| File | Change |
|---|---|
| `src/nexus/bricks/search/results.py` | `BaseSearchResult.title_score: float \| None = None` |
| `src/nexus/bricks/search/daemon.py` | `SearchDaemonConfig.title_arm: bool = True`; hybrid title-arm block + sub-fusion swap; `title_score` passthrough in `_coerce_to_search_result` (dataclass and dict branches) and `_fuse_ranked_results` copy list; `title_ms` in `_empty_backend_timing` |
| `src/nexus/server/lifespan/search.py` | `NEXUS_SEARCH_TITLE_ARM` env parse (same pattern as `NEXUS_SEARCH_PAGE_AGGREGATION`) |
| `src/nexus/server/api/v2/routers/search.py` | `_serialize_search_result`: emit `title_score` rounded to 4 places, omitted when None (compact pattern like `context` / `macro_text`) |
| `docs/surface-coverage/api-rpc-surface-coverage.yaml` | regen after router edit |

`fusion.py` needs no change — `rrf_multi_fusion` already exists. Zero schema
change, no reindex.

## Edge cases and error handling

- **No query embedding** (`qvec is None`): the existing keyword-only fallback
  runs; no title arm (the arm is hybrid-only per the issue).
- **locate returns nothing**: empty title arm.
  `rrf_multi_fusion([chunk, page, []])` is rank-identical to today's
  `rrf_fusion(chunk, page)` (same RRF sum and top-rank bonus) — guarded by a
  parity test. The only visible delta for non-title queries is the accepted
  `vector_score` mislabel fix.
- **`fetch_ranges` absent or raises**: hydration is best-effort — debug log,
  uncovered hits emit `chunk_text=""`. A hydration failure never fails the
  search.
- **Chunkless doc**: emitted with empty text (acceptance: stays retrievable).
- **Zone isolation**: `locate` filters `doc["zone_id"] == zone_id` strictly;
  `fetch_ranges` is zone-filtered. No cross-zone leakage.
- **Federated**: local zones inherit via `daemon.search()`; remote zones run
  their own daemon build; `title_score` rides the same result envelope as
  `splade_score`.
- **`path_filter`** is passed straight through as locate's `path_prefix`
  (both are prefix-match semantics).

## Testing

New `tests/unit/bricks/search/test_daemon_title_arm.py`, harness cloned from
`test_daemon_fusion_params.py` (`SearchDaemon.__new__` + explicit `.config`,
real-bool config guard):

1. **Acceptance fixture**: doc whose title matches the query but whose body
   chunks are weak → enters hybrid top-N with `title_score` set; same fixture
   with `title_arm=False` does not surface it.
2. **Non-title-query parity**: locate has no hits → results identical with the
   arm on and off.
3. **Chunkless doc** retrievable with `chunk_text=""`.
4. **Flag off**: `locate` never called.
5. **Hydration**: covered paths borrow (no `fetch_ranges` call); uncovered
   paths trigger exactly one batched call; `fetch_ranges` raising → search
   still returns.
6. **Fusion parity unit**: `rrf_multi_fusion([chunk, page])` rank-equals
   `rrf_fusion(chunk, page)` (guards the swap).
7. **Serializer** (`test_search_result_serialize.py`): `title_score` emitted
   rounded when set, key absent when None.
8. Existing suites stay green: `test_daemon_fusion_params.py`,
   `test_final_list_page_pooling.py`, `test_page_aggregation.py`, integration
   `test_fusion.py`.

## Rollout

Default on; `NEXUS_SEARCH_TITLE_ARM=false` disables for ablation or rollback.
No migration, no reindex.

## Acceptance mapping (issue #4545)

| Acceptance criterion | Covered by |
|---|---|
| Title-match doc with weak body chunks enters hybrid top-N | Test 1 |
| Non-title queries unchanged within tolerance | Tests 2 and 6; fusion is rank-based, title arm contributes only where it has hits |
| Attribution field marking title-arm participation | `title_score` |

## Dependencies

- Soft: #4541 fusion params (landed — final-stage knobs preserved by design).
- Benefits from #4542 per-doc pooling (landed — pooling runs after fusion,
  title-arm entries pool per path like any other).
- Hard: none.
