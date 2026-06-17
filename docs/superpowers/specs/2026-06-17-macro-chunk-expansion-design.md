# Macro-chunk (neighbor-context) expansion for hybrid search

- **Issue:** #4398
- **Status:** Design approved — pending implementation plan
- **Date:** 2026-06-17

## Summary

Add an opt-in, read-side **macro-chunk expansion** stage to hybrid search. We keep
indexing and ranking small chunks (good recall/precision), but when a caller sets
`expand=macro`, each returned hit is expanded into its surrounding **section** —
adjacent chunks by `chunk_index`, bounded by section headings, the file boundary,
and a token budget — and the stitched text is returned in an additive `macroText`
field next to the original `chunkText`.

Model-free, no new infrastructure.

## Motivation

Hybrid search ranking is at bench ceiling (`tests/benchmarks/gbrain_eval.py`:
recall@5 ≈ 0.9489 / NDCG@5 ≈ 0.9028 with RRF + top-rank bonus), so further *ranking*
work is low ROI. The remaining headroom is **answer-context quality**, which
recall@5 / NDCG@5 do not measure: we return ~500–1000 byte chunks, not the section
a consumer needs to answer.

The technique is to **decouple retrieval granularity from context granularity**:
index small chunks for precise matching, then at read time stitch each hit into its
surrounding section under a token budget, with a forward-biased variant for code so a
signature hit also pulls in the function body that follows.

`result_builders.py` already max-pools chunk *scores* to page granularity
(`_aggregate_chunks_to_pages`) but never stitches *content*. This adds the content
stitching.

## Goals / Non-goals

**Goals**
- Richer answer context for RAG consumers, opt-in, with no change to default behavior
  or ranking metrics.
- One shared, backend-agnostic, unit-testable expansion implementation across both
  FULL (pgvector) and SANDBOX (sqlite-vec) profiles.

**Non-goals (explicitly parked)**
- Cross-encoder / hosted reranker (hosting + latency cost; ~no lift at current ceiling).
- Multi-step / agentic retrieval.
- Knowledge-graph retrieval layer.

## Output contract

- New request param `expand` on `POST /api/v2/search/query`: enum `none` (default) |
  `macro`. Default → response byte-identical to today.
- When `expand=macro`, each hit gains an additive `macroText` field
  (`macro_text` internally → `macroText` over the API), plus `macroLineStart` /
  `macroLineEnd` for the covered line span. The short `chunkText` stays as the matched
  snippet / citation anchor.
- Additive field in the shared `nexus-client` `SearchHit` schema. Ranking,
  `gbrain_eval`, and existing callers are unaffected.

## Consumer context

The primary consumer is the external Koodle app. It calls
`services.nexusClient.search.query()` → `POST /api/v2/search/query` with
`{ q, type:"hybrid", limit (over-fetched ×5, cap 80), path:/workspaces/{id}/ }`, reads
`path, chunkText, score, chunkIndex, lineStart, lineEnd`, and feeds `chunkText`
straight into an LLM (a deepagents tool result). It **never re-fetches source or widens
context itself**, so server-side expansion is the only path to richer context for it.

Because that consumer is external and over-fetches heavily, expansion is **opt-in** —
to avoid token blow-up and to avoid doing neighbor-fetch work for callers that don't use
it. Koodle opts in by sending `expand=macro`, reading `macroText ?? chunkText`, and
lowering its over-fetch multiplier in the same change.

## Architecture (shared module + thin fetch primitive)

Pipeline in the daemon query path, only when `expand=macro`:

```
hybrid retrieve + RRF fuse                 (existing)
        ↓ ranked results [{path, chunk_index, chunk_text, score, ...}]
_aggregate_chunks_to_pages                 (existing, unchanged)
        ↓ final ordered chunk rows
macro_chunk.expand_results(rows, fetcher, cfg)   ← NEW, gated on expand=macro
        ↓ each row gains macro_text + line span
serialize → API: macro_text → macroText
```

Components:

1. **`macro_chunk.py`** (new, pure Python, zero DB knowledge):
   - `ExpansionConfig` — token budget `T`, window radius `W`, code forward-bias flag.
   - `NeighborFetcher` (Protocol) — `fetch_ranges(spans: list[(path, lo, hi)]) -> list[ChunkRow]`.
   - `ChunkRow` (dataclass) — `path, chunk_index, text, tokens, line_start, line_end, heading_prefix`.
   - `expand_results(rows, fetcher, cfg)` — derive anchors → merge overlapping
     `(path, index-range)` spans → one batched fetch → build per-path `index→row` map →
     run the expansion algorithm per anchor → attach `macro_text` + line span. All
     sub-steps are pure functions.

2. **Backend fetchers** (thin, one per profile): `PgNeighborFetcher`,
   `SqliteNeighborFetcher` implement `fetch_ranges` as a single batched SQL. They resolve
   `path → path_id` internally; the algorithm never sees storage identity.

3. **Daemon wiring** — one call site after page aggregation, behind the `expand` flag.
   No algorithm logic in `daemon.py`.

Data-flow decisions:
- Anchor identity = the result's existing `path` + `chunk_index` (no new identity threading).
- **One batched, range-merged fetch per query** — two hits in the same section share a span.
- Page aggregation runs **before** expansion, so we only expand chunks that survive to
  the final list (bounded work).
- `macro_text` is attached additively; `chunk_text` / `score` / ordering untouched.

## Expansion algorithm

**The "section" unifies heading-clipping and adaptive sizing.** A *section* is the
maximal contiguous run of chunks (same `path`) sharing the anchor's `heading_prefix`.
That one concept is both the clip boundary and the adaptive target.

Per anchor:

```
section = contiguous neighbors of anchor with heading_prefix == anchor.heading_prefix
          (bounded by file edge and the fetch radius W)

if section_tokens <= T:
    window = whole section                      # adaptive: size to the real section
else:
    window = budgeted sub-window within section, anchored on the hit:
        - prose: grow outward centered (backward/forward balanced) until T
        - code (path has a code extension): forward-bias — grow forward first
          (capture the body after the signature), then backward with leftover T

macro_text = concat(window chunks, ascending chunk_index, "\n"-joined)
line span  = (min line_start, max line_end) over the window
```

Caps (whichever binds first):
- `T` = `NEXUS_SEARCH_MACRO_CHUNK_TOKENS` — token budget, using stored `chunk_tokens`.
- `W` = `NEXUS_SEARCH_MACRO_CHUNK_WINDOW` — index radius (also the fetch radius); hard
  cap for pathological sections. Token budget normally binds first; `W` truncation is a
  safety rail and is logged (no silent cap).

**Small-section dedup:** group final anchors by `(path, section-span)`. For each group,
compute `macro_text` once from the top-ranked anchor and attach it to every member. No
N near-duplicate windows, no recompute; the consumer's existing `path+chunkIndex` dedup
collapses the rest if it wants.

Edge cases:
- `heading_prefix is None` (code without headings, plain text, non-markdown): no heading
  boundary → section bounded only by file edge + `T`/`W`. Code still forward-biases;
  prose centers.
- 1-chunk section → `macro_text == chunk_text` (no-op, still emitted for shape consistency).
- Section extends past fetch radius `W` → truncate at `W`, log it.
- Anchor chunk missing from the fetch (eventual-consistency gap) → skip expansion, leave
  `chunk_text` as-is; never error the search.

## Fetch primitive

`fetch_ranges(spans: list[(path, lo, hi)]) -> list[ChunkRow]` — **one batched query per
search**:
- Postgres: single SQL, `(path_id, chunk_index)` spans OR'd (or a VALUES join),
  `file_paths` join to resolve `path → path_id`; selects
  `chunk_index, chunk_text, chunk_tokens, line_start, line_end, heading_prefix`, ordered
  by `chunk_index`.
- SQLite (SANDBOX): same shape, SQLite dialect.

Open implementation detail to verify during planning: confirm the SANDBOX chunk store
persists `chunk_tokens` / `line_start` / `heading_prefix` (the Postgres `document_chunks`
writer in `chunk_store.py` does). If its table is leaner, the migration adds those columns
there too.

## Migration (persist `heading_prefix`)

Add `heading_prefix TEXT NULL` to the chunk table(s). The chunker already computes it
(`chunking.py`, #3719); the writer just stores it now.

**No blocking backfill:** existing rows have `NULL` `heading_prefix` → expansion treats
NULL as "no heading boundary" (degrades to a token/file-bounded window — still correct,
just less section-precise) until natural reindex. An optional reindex restores full
section precision.

## Config / defaults

- `expand` request param: `none` (default) | `macro`.
- `NEXUS_SEARCH_MACRO_CHUNK_TOKENS` (default 1024) — per-result token budget.
- `NEXUS_SEARCH_MACRO_CHUNK_WINDOW` (default 8) — index radius / fetch radius.
- Code forward-bias: on by default.

All server-side knobs are tunable against the benchmark.

## Error handling / observability

Expansion is **best-effort and never fails search**: any fetch error, missing chunk, or
edge condition → return the hit with `chunk_text` only. `expand_results` is wrapped in a
guard; degradation is logged and counted. Add `macro_expand_ms` timing plus a
truncation/fallback counter, consistent with existing daemon instrumentation.

## Test plan (TDD, failing-first)

- **Unit** (pure module, in-memory fake `NeighborFetcher`, no DB): whole-section-fits;
  section > budget centered (prose) vs forward-biased (code); heading boundary stops;
  `heading_prefix=None` path; 1-chunk no-op; section > `W` truncate + log; small-section
  dedup shares one window; range-merge collapses overlapping spans.
- **Integration** (both profiles): seed docs → `expand=macro` returns stitched
  `macroText`; default (no `expand`) response byte-identical to today.
- **Benchmark guard:** `gbrain_eval.py` recall@5 / NDCG@5 unchanged with expansion off
  *and* on (text-only addition); add a context-quality run on
  `benchmarks/longmemeval/run_retrieval.py`.
- Validate on a 10-item fixture first, then the full corpus.

## Rollout / coordination

1. Server: migration + fetcher + `macro_chunk.py` + daemon wiring + `expand` param +
   `macroText` response field. Default off → safe to ship independently.
2. Shared `nexus-client` `SearchHit` schema: add optional `macroText` /
   `macroLineStart` / `macroLineEnd`.
3. Koodle: send `expand=macro`, read `macroText ?? chunkText`, lower its over-fetch
   multiplier.

## Acceptance criteria

- [ ] Opt-in `expand=macro`; default response byte-identical to today.
- [ ] Neighbor stitch clips at `heading_prefix` change, file boundary, and token budget.
- [ ] `heading_prefix` persisted (migration); both profiles; graceful NULL degradation.
- [ ] Code forward-bias path.
- [ ] Adaptive section sizing + small-section dedup.
- [ ] `macroText` (+ line span) returned additively; `chunkText` unchanged.
- [ ] No `recall@5` / `NDCG@5` change on `gbrain_eval.py`.
- [ ] Context-quality delta measured on `gbrain_eval.py` + `longmemeval`.
- [ ] Works in FULL (pgvector) and SANDBOX (sqlite-vec).

## Related

#3773 / #3797 (RRF top-rank bonus), #3699 (search backend rework).
