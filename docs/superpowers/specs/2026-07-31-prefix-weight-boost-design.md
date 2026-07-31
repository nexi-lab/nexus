# Per-Prefix Ranking Weight via path_contexts (Source-Tier Boost) — Design

**Issue:** [#4544](https://github.com/nexi-lab/nexus/issues/4544)
**Date:** 2026-07-31
**Status:** Approved (design review with user)

## Problem

All path prefixes rank equally in hybrid search, but real corpora have
authority tiers — curated docs vs chat transcripts vs archived material.
There is no way to tell the ranker "this prefix is noisier than that one."

The per-prefix metadata infrastructure already exists: `path_contexts`
(longest-prefix-matched, per-zone LRU-cached, admin-CRUD-managed) and
`_attach_path_contexts` in the search daemon, which already walks every
result set to attach a `context` string.

## Decisions (from design review)

| Decision | Choice |
|---|---|
| Boost placement | `_attach_path_contexts` + conditional daemon over-fetch (covers backend, legacy, graph, batch paths uniformly) |
| Floor-ratio gate | Uplifts only (`w > 1.0`), default ratio 0.25, configurable, 0 disables. Demotions always apply |
| Weight validation range | 0.1 – 10.0 at API/CLI layer; NULL/absent ≡ 1.0 |

## 1. Schema + store

- **Migration:** additive alembic migration adding `weight FLOAT NULL` to
  `path_contexts`. No server default — `NULL ≡ 1.0` in code, so existing
  rows and new rows without a weight behave exactly as today. Downgrade
  drops the column.
- **Model:** `PathContextModel` gains the matching nullable `Float` column
  so `Base.metadata.create_all` (SQLite dev/test) stays in parity with the
  Alembic-managed production schema.
- **Record:** `PathContextRecord` gains `weight: float | None = None`.
- **Store:** `PathContextStore.upsert(zone_id, path_prefix, description,
  weight=None)` writes the column in both the PostgreSQL and SQLite
  `ON CONFLICT DO UPDATE` branches; `list()` selects it.
- **Cache:** no changes. Weight edits go through `upsert`, which bumps
  `updated_at`, so the existing `(row_count, max_updated_at)` zone
  fingerprint invalidates the `PathContextCache` correctly.
- **Lookup:** new `lookup_record_in_records(records, path) ->
  PathContextRecord | None` returning the full matched record; the existing
  `lookup_in_records` becomes a thin `.description` wrapper so current
  callers are untouched.

## 2. Boost application in `_attach_path_contexts`

For each result, after the longest-prefix match: attach `context` exactly
as today, then apply the weight `w = record.weight or 1.0`:

- `w == 1.0` → nothing. No stamp, no multiply — byte-identical behavior.
- **Floor-ratio gate (uplifts only):** if `w > 1.0` and
  `result.score < floor_ratio × top_score`, skip the boost. `top_score` is
  the max *pre-boost* score of the incoming batch, computed once per call.
  Rationale: metadata may reorder near-peers but must not lift weak matches
  past strong ones. Demotions (`w < 1.0`) always apply — demoting an
  already-weak hit is harmless and keeps the noisy-prefix acceptance test
  meaningful.
- **Apply:** `result.score *= w`; stamp `result.tier_boost = w`.
- **Attribution field:** new `tier_boost: float | None = None` on
  `BaseSearchResult`, alongside the existing `attribute_boost` /
  `original_score` precedent (#1092). `original_score` is NOT touched
  (already owned by attribute boosting); the pre-boost score is recoverable
  as `score / tier_boost`.
- **Idempotency:** results with `tier_boost` already set are skipped.
  `batch_search` re-runs the context lookup on results whose inner
  `search()` already attached — without this guard the weight would apply
  twice (`score × w²`).
- **Re-sort:** stable descending sort by `score`, executed only when at
  least one multiply happened. No weights applied → the list object and its
  order are untouched.
- The `batch_search` inline attach block stays context-only — inner `search()` calls already applied weights; re-applying against post-boost scores would re-evaluate the floor gate against a shifted top.
- Fail-soft contract unchanged: any per-result failure increments
  `path_context_attach_failures` and leaves that result unboosted.

## 3. Conditional over-fetch + trim (`_search_on_current_loop`)

The fusion stage trims to the daemon `limit` inside `_search_via_backends`
(`fuse_results(..., limit=limit)`), so a boost applied at attach time could
never *promote* a below-cutoff hit without a wider candidate pool.

At the top of `_search_on_current_loop`:

1. Resolve the path-context cache → `refresh_if_stale(effective_zone)` →
   `snapshot_zone` → `has_weights = any(rec.weight not in (None, 1.0))`.
   Fail-soft: any exception → `has_weights = False` (search never breaks
   on a weight-lookup problem; attach still runs its own fail-soft pass).
2. **`has_weights == True`:** every candidate fetch in the method
   (`_search_via_backends`, legacy `_keyword_search` / `_hybrid_search` /
   `_semantic_search`) uses `limit × tier_boost_overfetch_factor`
   (default 3). Each return point runs attach (boost + re-sort), then trims
   back to the caller's `limit`. The route-level ReBAC ×3 over-fetch
   composes on top unchanged (worst case daemon pool = limit × 9).
3. **`has_weights == False`:** today's exact pipeline — no wider fusion
   window, no `page_aggregation` composition change, no re-sort. The
   acceptance criterion "weight unset/1.0 everywhere is byte-identical to
   today" holds trivially because the code path is identical.

Cost: the snapshot work is the same work attach pays later in the same
request; the incremental cost is one cache-fingerprint check per search,
and the wider fetch only occurs for zones that actually configured weights.

Pre-filter stage (adversarial-review amendment): weights — including the
floor gate's `top_score` — are computed over the daemon's candidate pool,
BEFORE route-level ReBAC filtering, in the same pipeline stage as RRF
fusion ranks, attribute boost (#1092), and page pooling (#4542), all of
which already depend on later-filtered candidates. Relocating weight
finalization post-ReBAC would contradict the attach-point placement and
break federated composition (remote zones weight server-side, before the
local caller's filter). Accepted trade-off.

Bounded approximation (adversarial-review amendment): weights re-rank
within the widened window of `limit × factor` raw candidates only — if
more than `limit × factor` raw hits from a demoted prefix outscore an
unweighted hit, that hit stays outside the window and cannot be promoted.
Exact weighted top-K would require pushing weights into every backend
query; this design explicitly trades that away for the attach-point
approach. Weights only mutate ranking when the pool was actually widened
(factor ≥ 2 and probe succeeded); otherwise they are suppressed and
counted in `tier_boost_suppressed_searches` / `tier_boost_probe_failures`
(exposed via search stats).

Macro expansion (`expand=macro`, #4398) runs in `search()` after the
trimmed list is returned — unaffected.

## 4. API + CLI surface

- `PathContextIn` gains `weight: float | None = Field(default=None,
  ge=0.1, le=10.0)`. PUT `/api/v2/path-contexts/` persists and echoes it.
- GET list emits `weight` for every row (`null` = unset ≡ 1.0) — admin
  surface favors explicitness.
- DELETE and the zone-scoping/permission rules are unchanged.
- `_serialize_search_result` emits `tier_boost` (rounded to 4 places) only
  when set — same compact-response convention as `context`.
- CLI `nexus path-context` upsert gains `--weight`; list output shows the
  weight column.
- Regenerate `api-rpc-surface-coverage` YAML after the router edit
  (#4541 lesson: surface regen is required on any search-adjacent router
  change).

## 5. Federated path

Correct by construction because the boost lives in the daemon:

- Local zones: `FederatedSearchDispatcher._search_zone` calls
  `daemon.search(zone_id=zone)` — the owning zone's weights are applied
  before the federated merge.
- Remote zones: the remote node's daemon boosts before the RPC returns;
  scores arriving over the wire already carry the owning zone's weights.
- `_merge_by_raw_score` compares boosted scores, so cross-zone ordering
  reflects per-zone tiers; the merge happens before the federated final
  trim, satisfying the ordering constraint.
- Extend the `_strip_none_context` pattern so `tier_boost: null` never
  leaks onto federated result dicts (same Round-5/6 hygiene as `context`).
- Remote *attribution* (the `tier_boost` field traveling over the RPC) is
  best-effort: score ordering is the contract. Verify the remote RPC search
  serializer during implementation; if the field doesn't survive the wire,
  ordering is still correct and the field is simply absent.

## 6. Config knobs

Two new `DaemonConfig` fields with env overrides, following existing
search-knob conventions:

| Field | Default | Env | Meaning |
|---|---|---|---|
| `tier_boost_overfetch_factor` | 3 | `NEXUS_SEARCH_TIER_BOOST_OVERFETCH` | Candidate-pool widening multiplier when a zone has weights |
| `tier_boost_floor_ratio` | 0.25 | `NEXUS_SEARCH_TIER_BOOST_FLOOR_RATIO` | Uplift gate: skip `w > 1.0` when `score < ratio × top`; 0 disables |

## 7. Testing

- **Store/migration:** upsert-with-weight roundtrip (PG + SQLite branches),
  NULL → `None`, migration parity tests (model vs alembic head).
- **Attach unit tests:** demotion reorders below equal-relevance peers;
  promotion works through the over-fetch pool; floor gate blocks uplift of
  far-below results; gate does not block demotion; double-attach is
  idempotent (no `w²`); no-weight case leaves the result objects, scores,
  and list order untouched; `tier_boost` stamped only when `w ≠ 1.0`.
- **Daemon integration:** seeded weights end-to-end through
  `_search_on_current_loop` with fake backends, including trim-back-to-limit
  after over-fetch and timing-field preservation.
- **Router:** weight bounds validation (0.05 and 10.5 rejected), PUT echo,
  list emission, `_serialize_search_result` emits `tier_boost` only when set.
- **Federated:** local-zone weight visible in merged ordering;
  `tier_boost: null` stripped from federated dicts.
- **Byte-identity regression:** full existing search suite green with no
  weights configured.

## Acceptance (from the issue)

- Seeding `weight=0.5` on a noisy prefix demotes its hits below
  equal-relevance hits from weight-1.0 prefixes; `weight` unset/1.0
  everywhere is byte-identical to today.
- Boost visible per-result (`tier_boost` attribution); federated path
  applies the owning zone's weights.

## Out of scope

- Per-query weight overrides (weights are admin/zone state, not request
  params).
- Weight semantics in Zoekt/grep/glob/locate paths (no `path_contexts`
  attach there today).
- RPC surface versioning to carry `tier_boost` attribution to older remote
  nodes (#4541 precedent: remote nodes reject unknown params; score-level
  correctness does not depend on it).
