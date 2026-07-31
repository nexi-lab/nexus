# Search Recency Decay — Query-Conditional Post-Fusion Time Boost (Issue #4543)

**Date:** 2026-07-31
**Issue:** [#4543](https://github.com/nexi-lab/nexus/issues/4543)
**Status:** Approved

## Problem

Recency has zero influence on ranking. Nothing in the query path reads any
timestamp — a 3-year-old chunk and yesterday's chunk with equal text relevance
tie. Deployments indexing living corpora (notes, mail, docs) systematically
surface stale material.

## Decision summary

A multiplicative, post-fusion recency boost applied at a **single chokepoint in
`SearchDaemon.search()`**, with mtimes obtained by **one batch hydration query**
against `file_paths` (served index-only by the existing covering index
`idx_file_paths_zone_path_covering`). Default **auto** (post-review revision;
originally shipped default-off): zero-config deployments boost recency-intent
queries ("latest", "recent", "today"…) while neutral queries stay
byte-identical via the intent gate. Opt out per-deployment with
`NEXUS_SEARCH_RECENCY=off` or per-request with `recency=off`.

Two alternatives were considered and rejected:

1. **Carry `fp.updated_at` in the backend SELECTs** (the issue's original
   sketch). Rejected because exploration found coverage gaps: sqlite_vec's
   vec0 virtual table has no `file_paths` join (semantic search on SQLite
   would be entirely unboosted), the legacy fallback stack (BM25S/Zoekt)
   never touches those SELECTs, and the new field would have to survive both
   the fusion first-leg-wins merge (`fusion.py:151/161`) and the
   `_coerce_to_search_result` field whitelist (`daemon.py:2183`).
2. **Carry + hydrate misses.** Full coverage but two code paths to test and
   keep in sync; the hydration query alone is already ~free.

Hard rule (from the issue, honored): decay never goes into SQL `ORDER BY` — it
would corrupt the rank positions RRF consumes, and the vector arm's HNSW scan
must stay pure-distance to remain index-usable. This design changes **no SQL
ordering anywhere**.

## 1. Core mechanism — new module `src/nexus/bricks/search/recency.py`

- `apply_recency_boost(results, mtimes, *, weight, half_life_days, now)` —
  pure function over the typed `SearchResult` list:
  - For each result with a known mtime and `score > 0`:
    `boost = 1 + weight * H / (H + age_days)` (hyperbolic decay; `H` =
    half-life days), then `score *= boost` and `recency_boost = boost`
    (attribution).
  - `age_days` is fractional, clamped `>= 0` (future mtimes from clock skew
    get the max boost, never a penalty). Naive `updated_at` values are
    treated as UTC.
  - Results without an mtime row (deleted mid-flight, remote-zone results,
    unknown paths) keep their score untouched and `recency_boost = None`.
  - Non-positive scores are skipped defensively (a multiplicative boost on a
    negative score would demote).
  - Boost is `> 1` and monotone-decreasing in age, so it strictly promotes
    newer material and never reorders by demotion; RRF/BM25/cosine score
    domains (all positive) compose safely.
  - Followed by an **in-place** re-sort by score descending (preserves the
    `SearchResultList` subclass and its `search_timing`).
- `has_recency_intent(query)` — `set(query.lower().split()) & RECENCY_WORDS`,
  mirroring the query router's `word_set & TEMPORAL_WORDS` style.
- `RECENCY_WORDS: frozenset[str]` lives in `contracts/search_types.py` next to
  `TEMPORAL_WORDS`:
  `{"latest", "newest", "recent", "recently", "current", "currently",
  "today", "yesterday", "now", "new"}`.
  Deliberately **not** `TEMPORAL_WORDS` — that set signals temporal
  *complexity* ("history", "before", "until" often want OLD documents) and
  lacks every actual recency word.

## 2. Wiring — single chokepoint in `SearchDaemon.search()` (daemon.py:1784)

`search()` gains three keyword params (mirroring the #4541 knob style):

- `recency: str | None = None` — `"off" | "on" | "auto"`; `None` defers to
  `DaemonConfig.recency_mode`.
- `recency_weight: float | None = None` — `None` defers to config.
- `recency_half_life_days: float | None = None` — `None` defers to config.

After `_search_on_current_loop` returns and **before** the existing
`_apply_macro_expansion` post-hook at daemon.py:1823 (the #4398 precedent for
post-search enrichment; running the boost first keeps all ordering logic ahead
of enrichment, though macro expansion is per-result and order-insensitive):

1. Resolve `None` params against `DaemonConfig` defaults.
2. `active = (mode == "on" or (mode == "auto" and has_recency_intent(query)))
   and weight > 0`.
3. If active: one batch query
   `SELECT virtual_path, updated_at FROM file_paths WHERE zone_id = :z AND
   virtual_path IN (:paths...) AND deleted_at IS NULL`
   via `self._async_session`, expressed as a SQLAlchemy `select()` on
   `FilePathModel` so both PG and SQLite return real datetimes. Served
   index-only by `idx_file_paths_zone_path_covering`
   (`storage/models/file_path.py:92-98`) — sub-ms for a `limit×3` result list.
4. `apply_recency_boost(...)` + re-sort.

**Fail-soft:** any hydration exception → `logger.warning` + increment a new
`recency_attach_failures` counter on `DaemonStats` (mirroring
`path_context_attach_failures`, daemon.py:137) + return unboosted results.
Search never 500s on a recency bug.

**Untouched:** `_search_on_current_loop`, `_search_via_backends`, all backend
SELECTs, fusion, `_coerce_to_search_result`. Because the boost runs after
coercion on typed results (the macro_text trick), the whitelist chokepoint is
irrelevant. The boost acts on the route's over-fetched list (`fetch_limit =
limit × 3` for the ReBAC filter), so recency can promote items into the
user-visible window before the caller's final trim.

Coverage for free: all three search types (keyword / semantic / hybrid), both
DB backends including sqlite_vec dense-only results, and the legacy fallback
stack — everything flows through `search()`.

## 3. Config — `DaemonConfig` (daemon.py:186), macro_chunk-style env wiring

Using the `field(default_factory=lambda: _get_env_*(...))` pattern at
daemon.py:297-305 (self-contained; no lifespan edit needed):

| Field | Default | Env var |
|---|---|---|
| `recency_mode` | `"auto"` | `NEXUS_SEARCH_RECENCY` (`off`/`on`/`auto`) |
| `recency_weight` | `0.3` | `NEXUS_SEARCH_RECENCY_WEIGHT` |
| `recency_half_life_days` | `30.0` | `NEXUS_SEARCH_RECENCY_HALF_LIFE_DAYS` |

Requires a small `_get_env_float` helper next to the existing `_get_env_int` /
`_get_env_bool`.

(Post-review revision: `recency_mode` originally defaulted to `"off"`; it now
defaults to `"auto"` so deployments get recency handling with zero config.
The intent gate preserves byte-identity for neutral queries; operators opt
out with `NEXUS_SEARCH_RECENCY=off`.)

Enable/disable is carried by the **mode**, not `weight = 0`, so the default
gives sensible behavior (×1.3 max boost, 30-day half-life) without an operator
having to pick a weight. `recency_weight` defaults to 0.3: a fresh document
gets ×1.3, a half-life-old one ×1.15, decaying toward ×1.

Note: `SearchConfig` (`bricks/search/config.py`) fusion fields are vestigial —
nothing on the daemon path reads them. Recency config goes **only** on
`DaemonConfig`.

## 4. API surface — `src/nexus/server/api/v2/routers/search.py`

Following the #4541 (`alpha`/`fusion`/`rrf_k`) and #4398 (`expand`) patterns:

- New flat `Query` params on `GET /api/v2/search/query`:
  - `recency: str | None = Query(None, ...)` — validated against
    `{"off", "on", "auto"}` with the hand-rolled 400 pattern
    (search.py:342-352). `None` = defer to daemon config.
  - `recency_weight: float | None = Query(None, ge=0.0, le=5.0)`
  - `recency_half_life_days: float | None = Query(None, gt=0.0, le=3650.0)`
- Forwarded in both the single-zone branch (through
  `_handle_single_zone_search` → `daemon.search`) and the federated branch.
- `_serialize_search_result` (search.py:156-188): emit `recency_boost` only
  when non-`None` (the `macro_text` conditional-emit pattern) so responses
  where the boost did not fire stay **byte-identical**.
- `BaseSearchResult` (`bricks/search/results.py`) gains
  `recency_boost: float | None = None` — the only new dataclass field. No
  `mtime` field on results (YAGNI; attribution only, per acceptance criteria).
- `SearchBrickProtocol.search` (`contracts/protocols/search.py:48-59`) gains
  the three params.
- Surface-coverage YAML regenerated
  (`uv run python scripts/gen_api_surface_coverage.py`) since the `/query`
  decorator line shifts.

## 5. Federated — `src/nexus/bricks/search/federated_search.py` (rrf_k precedent)

- `search` / `_search_impl` / `_search_zone` thread the three knobs.
- **Cache key** (`_make_cache_key`, line ~449) appends all three — the #4541
  lesson: requests differing only by recency settings must not share cache
  entries.
- Local zones: knobs forwarded to `daemon.search(...)` (boost fires per local
  zone; boosted raw scores merge coherently in `_merge_by_raw_score`).
- Remote zones: RPC params dict **unchanged** — older remote nodes reject
  unknown params. Remote results are simply unboosted (they also lack local
  `file_paths` rows for hydration). Documented in the `_search_impl`
  docstring exactly like the rrf_k caveat at lines 577-578.
- Federated serializer `_result_to_dict` emits all dataclass fields verbatim;
  add a `None`-strip for `recency_boost` mirroring `_strip_none_context` so
  the federated wire doesn't leak `recency_boost: null`.

## 6. Testing

- `tests/unit/bricks/search/test_daemon_recency.py` (canonical, mirrors
  `test_daemon_fusion_params.py`'s fake-backend harness):
  - boost math (fresh ×(1+w), half-life-old ×(1+w/2), ancient →×1);
  - near-duplicate fixture at different mtimes: recency-on ranks newer first,
    **a neutral-query default request produces identical results**
    (acceptance), and a zero-config recency-intent query boosts (default
    auto);
  - `auto` + recency-word query fires; `auto` + neutral query does not;
  - missing mtime row → unboosted, `recency_boost is None`;
  - hydration DB failure → fail-soft, counter incremented, results returned;
  - non-positive score skipped;
  - re-sort preserves `SearchResultList.search_timing`.
- `tests/unit/server/api/v2/test_search_recency_param.py` (mirrors
  `test_search_expand_param.py`): params accepted; bogus `recency` → 400;
  out-of-bounds weight/half-life → 422; defaults forwarded as `None`;
  explicit values forwarded to `daemon.search`.
- Extend `tests/unit/bricks/search/test_search_result_serialize.py`:
  `recency_boost` omitted when `None`, emitted when set.
- Extend `tests/integration/bricks/search/test_federated_search.py`: cache key
  includes recency knobs; local-zone forwarding.
- Extend `tests/integration/bricks/search/test_brick_protocol.py`: protocol
  signature conformance.

## 7. Out of scope

- Remote-zone recency (needs RPC param versioning — follow-up if wanted).
- The RPC `handle_search` handler (`rpc/handlers/filesystem.py:117-128`) —
  already a knob dead-end (never carried alpha/fusion/rrf_k).
- The graph branch (`graph_enhanced_search`) — same pre-existing gap as
  alpha/fusion_method.
- The batch endpoint (`POST /query/batch`) — matches #4541, which also skipped
  it.
- Legacy `_hybrid_search` internals — hydration boosts its *output*, no
  internal changes.
- Exposing per-result `mtime` on the wire.
- DB migrations — none needed; `file_paths.updated_at` and its covering index
  already exist.

## Known limitations

- `file_paths.updated_at` bumps on ANY row update (SQLAlchemy `onupdate`),
  including re-index bookkeeping writes (`indexed_content_id` /
  `last_indexed_at`), not just content writes. A bulk re-index uniformly
  freshens every document — order-preserving for ranking, but it dilutes
  `auto`-mode discrimination and makes `recency_boost` attribution read
  fresher than the content is until timestamps diverge again. A
  content-write-only timestamp is a possible follow-up.
- With recency active, boosted local-zone scores compete against unboosted
  remote-zone scores in the federated raw-score merge — local zones are
  systematically advantaged. Inherent to the local-only scope (rrf_k RPC
  precedent); revisit with RPC surface versioning.
- The graph branch (`graph_enhanced_search`) ignores the recency knobs —
  the same pre-existing gap as `alpha`/`fusion_method`.
- With the default now `auto`, `RECENCY_WORDS`' inclusion of `"new"` can fire
  on lexical false positives ("new user registration flow"). At weight 0.3
  the distortion is a mild ×≤1.3 promote-only nudge; drop `"new"` from the
  set if auto-mode surprises show up in practice.

## Acceptance criteria (from the issue)

1. Fixture with near-duplicate content at different mtimes: temporal query
   ranks newer first with recency on; a request where the boost does not
   fire is byte-identical. ✔ covered by `test_daemon_recency.py`.
   (The issue asked for default-off; revised post-review to default-auto so
   deployments need no config — byte-identity is now scoped to neutral
   queries and explicit `recency=off`.)
2. Per-result attribution field (`recency_boost`) set only when the boost
   fired. ✔ covered by serializer + daemon tests.

## Dependencies

- Soft dependency from the issue (fusion-params knob plumbing, #4541) already
  landed in PR #4551 — this design reuses that exact plumbing path.
