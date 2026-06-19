# Search Mutation Consumer Parking — Design (Issue #4337)

**Date:** 2026-06-09
**Issue:** [#4337](https://github.com/nexi-lab/nexus/issues/4337) — search daemon: unresolvable mutation event blocks checkpoint forever (head-of-line, infinite 2s retry)
**Companion context:** [#4339](https://github.com/nexi-lab/nexus/issues/4339) — payload integrity loss made a class of paths permanently unreadable; those paths' write events are the observed poison events.

> **Post-#3699 note (2026-06-10):** this spec was written against the
> four-consumer daemon (`bm25`, `fts`, `embedding`, `txtai`). Issue #3699
> later removed the txtai/bm25s stack, so the shipped implementation gates
> the remaining two consumers (`fts`, `embedding`) — see
> `MUTATION_CONSUMER_NAMES` in `daemon.py`. The design is otherwise
> unchanged; consumer-count references below are historical.

## Problem

The four durable search mutation consumers (`bm25`, `fts`, `embedding`, `txtai`) in
`src/nexus/bricks/search/daemon.py` each tail the operation log from a persisted
per-consumer checkpoint. `_run_mutation_consumer` fetches a batch, runs the
handler, and checkpoints the last sequence number only if the handler returns.

When the `MutationResolver` cannot obtain content for an UPSERT
(`content_resolved=False`), the bm25/fts/embedding handlers raise, so the
checkpoint never advances and the same batch is refetched every
`mutation_poll_seconds` (2s) forever. One permanently unresolvable event
(e.g. a #4339 victim path) therefore head-of-line blocks **all** subsequent
search indexing for that consumer. The txtai handler has the opposite defect:
it silently drops unresolved upserts with no trace.

Two root deficiencies:

1. **No retry bound.** "Refuse to checkpoint" is correct for transient
   failures but has no escape for permanent ones.
2. **No failure classification.** `MutationResolver._read_content` swallows
   every exception and returns `None`, so "file is gone" (permanent) is
   indistinguishable from "backend timeout" (transient).

## Goals (from issue asks)

1. Bounded retries per event; on exhaustion, **park** the event durably
   (skip + record + metric) instead of wedging.
2. Classify resolution failures permanent vs transient; fail fast on
   permanent.
3. Admin surface + runbook to unwedge (`skip event N`) and to re-drive or
   discard parked events — no settings-store surgery.

Non-goals: changing the operation-log schema, reworking checkpoint storage,
handling non-content handler failures (e.g. backend delete errors — verified
that `delete_document` on an absent doc returns `True`, so a parked upsert
followed by a delete cannot re-wedge), or wiring alert rules into a specific
deployment (runbook documents what to alert on).

## Approach (chosen: resolve-step chokepoint)

All four handlers already funnel through `SearchDaemon._resolve_mutations`.
The gate lives there, parameterized by consumer name. Alternatives considered
and rejected: per-consumer gate helper (4 duplicated call sites, txtai needs a
new branch); loop-level exception protocol in `_run_mutation_consumer`
(one-park-per-pass convergence, batch prefix re-execution,
exception-as-control-flow).

## Design

### 1. Resolver failure classification (`mutation_resolver.py`)

- `ResolvedMutation` gains:
  - `failure_kind: Literal["permanent", "transient"] | None = None`
  - `failure_detail: str | None = None`

  Set only when `content_resolved=False`.
- `_read_content` returns content plus classification instead of swallowing
  exceptions:
  - `FileNotFoundError` family (includes `NexusFileNotFoundError`) on **both**
    the scoped-path and virtual-path reads → `permanent`.
  - Any other exception, or a non-`str` return → `transient`.
  - `file_reader is None` → `transient`. (The reader is attached after boot by
    the server lifespan via `set_file_reader`; the boot window must never
    park events.)
- The `content_cache` DB fallback clears the failure when it hits. On a miss,
  the read's classification stands. Errors raised by the DB lookup itself
  continue to propagate (existing whole-batch retry path, unchanged — the
  gate never sees them).
- **Unresolved mutations are no longer cached.** Today the 30s TTL cache
  would serve a stale unresolved entry to most retry passes, making "N
  attempts" mean ~N/15 real read attempts. Not caching failures makes
  attempt budgets honest; the cost (one failed read per 2s pass per poisoned
  event) is exactly today's steady-state load.

### 2. Parking gate (`daemon.py`, inside `_resolve_mutations`)

- Signature becomes `_resolve_mutations(consumer_name, events)`. The four
  consumer handlers pass their name; the legacy hook-driven refresh path
  passes `"legacy-refresh"` and is exempt from the gate (it only resolves
  DELETE events, which are always resolved).
- In-memory attempt counts: `dict[(consumer_name, event_id), int]`,
  incremented once per pass that observes the event unresolved. Restart
  resets counts; a poisoned event then re-accumulates to its budget within
  seconds and re-parks. Accepted trade-off for not persisting counters.
  Hygiene: a counter is dropped when its event parks **or** resolves
  (recovered transient), so the dict stays bounded by the current batch.
- Budgets on `SearchDaemonConfig`:
  - `mutation_unresolved_permanent_attempts: int = 3` (~6s at 2s poll —
    absorbs cross-process visibility races despite op-log events being
    post-success)
  - `mutation_unresolved_transient_attempts: int = 30` (~60s — rides out
    brief backend blips; longer outages park and are re-driven later)
- Per pass, over the resolved batch's unresolved UPSERTs:
  1. Events at/over budget → park (persist record, bump metrics, WARNING
     log with event_id/path/kind/attempts), remove from the returned list,
     clear their counters.
  2. If any unresolved upsert remains under budget → raise
     `UnresolvedMutationError` (subclass of `RuntimeError`) naming event_id,
     path, kind, attempt/budget. `_run_mutation_consumer`'s existing except
     branch logs it and retries the batch — current semantics preserved.
     With mixed-budget batches this converges in at most
     `max(budgets)` passes.
- When the handler finally returns, the checkpoint advances past the parked
  events naturally.
- The consumers' four unresolved-content branches (three `raise` sites, one
  txtai silent drop) are **deleted**; the gate guarantees handlers only see
  resolved mutations. txtai thereby joins the same policy (its silent data
  loss becomes a visible parked record).
- If persisting a park record fails (settings store **and** file fallback),
  the gate raises instead of filtering: the batch retries and parking is
  re-attempted next pass. We never skip an event we failed to record.

### 3. Park store (mirrors checkpoint dual persistence)

- Primary: settings store key `search_mutation_parked` (single JSON
  document `{consumer_name: [entry, ...]}`); fallback file
  `mutation-parked.json` next to `mutation-checkpoints.json`. Same
  store-or-file degradation pattern and an `asyncio.Lock` as checkpoints.
- Entry fields: every field needed to reconstruct the
  `SearchMutationEvent` (`event_id`, `operation_id`, `op`, `path`,
  `new_path`, `zone_id`, `timestamp`, `sequence_number`) plus
  `kind`, `detail`, `attempts`, `parked_at`.
- Keyed by `(consumer, event_id)`; re-parking the same event replaces its
  entry.
- Cap `mutation_parked_max_entries: int = 200` per consumer. Overflow
  FIFO-evicts the oldest entry with an ERROR log and an eviction counter —
  a #4339-scale storm (thousands of dead paths) must not bloat the settings
  row; the storm itself is surfaced by metrics.
- Loaded at daemon start to seed the gauge and stats.

### 4. Metrics + stats

- New `src/nexus/bricks/search/consumer_metrics.py` (precedent:
  `src/nexus/bricks/auth/consumer_metrics.py`), direct import of `prometheus_client` (a hard dependency — matches the auth-brick precedent):
  - `nexus_search_mutation_parked_total{consumer, kind}` (Counter)
  - `nexus_search_mutation_parked_current{consumer}` (Gauge — set on park,
    unpark, discard, boot load; not named `..._parked` because
    prometheus_client strips `_total` from Counter names, so the Counter
    above already claims the `..._parked` base name in the registry)
  - `nexus_search_mutation_unresolved_retries_total{consumer, kind}`
    (Counter — one increment per retry pass; sustained rate is the
    early-warning signal before anything parks)
  - `nexus_search_mutation_parked_evicted_total{consumer}` (Counter)
- `stats.mutation_consumers[<name>]` gains `parked_count`,
  `last_parked {event_id, path, kind, parked_at}`, and
  `retrying {event_id, attempts, budget, kind} | None` — the
  lowest-sequence event currently under retry (the head-of-line blocker)
  when several are in flight — all surfaced by the existing
  `GET /api/v2/search/stats`.

### 5. REST admin surface (`src/nexus/server/api/v2/routers/search.py`)

Admin-gated with the same auth dependency as the router's existing
mutating/admin endpoints.

- `GET /api/v2/search/parked` → `{consumer: [entries]}`.
- `POST /api/v2/search/parked/retry` body
  `{"consumer": str, "event_ids": [str] | null}` (null = all for that
  consumer) → for each entry: reconstruct the event, run it one-shot through
  that consumer's handler; success → remove from park store; failure → keep
  entry, report. Response
  `{"retried": n, "succeeded": [...], "failed": [{"event_id", "error"}]}`.
  No checkpoint interaction — parked events are already behind the
  checkpoint. Concurrent live-consumer activity is safe: index upserts are
  idempotent. (Embedding handler no-ops without a provider, matching live
  behavior.)
- `POST /api/v2/search/parked/discard` body
  `{"consumer": str, "event_ids": [str]}` → remove entries without retrying
  (operator accepts the loss, e.g. after #4339 forensics).
- `POST /api/v2/search/consumers/{name}/skip-to` body `{"sequence": int}` →
  force-advance the persisted checkpoint. Validates the consumer name and
  `sequence > current`; deliberately does **not** cap at the current op-log
  max (an admin may pre-advance past a known-bad range) — the runbook warns
  that everything ≤ the new sequence is skipped for that consumer. Returns
  `{"previous": int, "current": int}`. This is the issue's "skip event N"
  escape hatch and also covers any future poison class the gate doesn't
  classify.
- Hardening: `_save_consumer_checkpoint` becomes monotonic
  (`max(current, new)`) so an in-flight consumer pass that succeeds with a
  stale batch cannot rewind a forced advance.

### 6. Runbook (`docs/operations/search-mutation-parking.md`)

Covers: symptom log lines (the exact "refusing to checkpoint" / parked
WARNING formats), diagnosis via `GET /api/v2/search/stats` and
`GET /api/v2/search/parked`, decision guide (retry after payload restore vs
discard), curl examples for retry/discard/skip-to, and suggested alert rules
(page on `nexus_search_mutation_parked_total` increase; warn on sustained
`nexus_search_mutation_unresolved_retries_total` rate).

### 7. Testing

- **Resolver unit tests:** classification matrix (NotFound both reads →
  permanent; NotFound then non-NotFound → transient; reader None →
  transient; cache hit clears failure; empty string still resolved),
  unresolved results not cached.
- **Gate unit tests:** budget exhaustion parks + filters + clears counter;
  under-budget raises `UnresolvedMutationError`; mixed batch (one permanent,
  one transient, one healthy) converges with checkpoint advance; park
  persistence failure raises instead of skipping; legacy-refresh exemption.
- **Park store tests:** settings-store and file-fallback round-trips,
  dedupe/replace, cap eviction order, boot load.
- **Daemon integration tests:** simulated poison event wedges then
  auto-unwedges after budget with checkpoint advanced past it; restart
  re-parks within budget; retry endpoint unparks on success and keeps on
  failure; skip-to monotonic guard under a concurrent stale checkpoint
  write.
- **API tests:** auth required, unknown consumer 4xx, `sequence <= current`
  4xx, happy paths.

Scope check: single brick (`bricks/search`) + one router + one runbook doc.
No kernel-internal (`rust/kernel/src/core/`) changes — no ownership concerns.

## Decisions log

| Decision | Choice | Why |
| --- | --- | --- |
| DLQ shape | Parked-record + re-drive (settings store/file; no DB migration) | Replayable after payload restore; avoids migration weight; matches checkpoint persistence pattern |
| Retry budgets | permanent 3 / transient 30 passes, in-memory | Fast unwedge (6s/60s); restart re-accumulates quickly; parked re-drive covers long outages |
| Admin surface | REST + runbook (CLI deferred) | Host CLI unreliable in some envs; REST is the dependable path |
| txtai | Unified under the same gate | Silent data loss → visible parked record; one policy for all four consumers |
| Gate location | `_resolve_mutations` chokepoint | One policy point; deletes four divergent per-consumer branches |
