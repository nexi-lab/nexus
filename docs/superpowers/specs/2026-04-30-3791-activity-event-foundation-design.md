# Activity Event Foundation — Design Spec (Issue #3791, Foundation Slice)

**Issue:** [#3791 — feat(P3-6): activity event stream + audit log + /metrics](https://github.com/nexi-lab/nexus/issues/3791)
**Date:** 2026-04-30
**Scope:** Foundation slice only. TUI (`nexus term`), `nexus logs --follow`, `nexus stats`, hash-chained audit, OTEL exporter — deferred to follow-up specs.

## Context

Issue #3791 calls for first-class server-side observability across hub-mode Nexus deployments: structured activity events, hash-chained audit, Prometheus metrics, operator TUI, and CLI surfaces (`logs`/`audit`/`stats`). With #3784 (hub mode) and #3785 (zone scoping) closed, operators need a way to see who is doing what across many connected agents.

This spec covers the **foundation slice** that everything else consumes — the activity event schema, in-process emitter, durable SQLite sink, and the metrics catalog. Downstream slices (TUI, log/stats CLI, hash-chained audit, OTEL) build on this contract and will be specced separately to avoid churning the schema mid-build.

## Goals (foundation slice)

1. Define a stable `ActivityEvent` schema covering the issue's six event kinds (`search`, `fetch`, `mcp_tool_call`, `zone_access`, `policy_block`, `approval`).
2. Provide an in-process emission API callable from any Nexus service with no hot-path performance impact.
3. Persist events to a durable, queryable SQLite store (default sink).
4. Expose the issue's Prometheus metric catalog at the existing `/metrics` endpoint.
5. Wire emission at five concrete callsites so downstream slices have real data to consume.

## Non-goals (explicit)

- Operator TUI (`nexus term`) — separate slice.
- `nexus logs --follow`, `nexus stats` CLI — separate slice (consumers of this sink).
- Hash-chained tamper-evident audit log + `nexus audit export --from --to` — separate slice. Activity events are observability, not legal record.
- OTEL/JSONL/webhook sinks — separate slice (multi-sink config schema).
- `fetch` event kind on the record_store read path — deferred. Fetch via MCP `read_file` tool will be observable as `mcp_tool_call` in foundation; a true `fetch` kind covering kernel-level reads ships with the audit slice.
- Phase-labelled `nexus_search_latency_seconds{phase=…}` — needs search-internals plumbing; ships with a follow-up slice.

## Survey of existing infra (why this is a new subsystem)

| Module | Purpose | Why not extend |
|---|---|---|
| `services/event_bus` + `services/event_log` | FileEvent (FS CRUD) pub/sub + transactional outbox + Kafka/NATS/PubSub exporters | File-op semantics, not search/tool/policy. Different schema, different consumers. |
| `services/audit_node` + Rust `install_audit_hook` | Kernel-level WAL audit; `/audit/traces/` DT_STREAM raft-replicated, drained into central zone | Wrong layer (sys_write traces, not app ops). Reused later for hash-chained audit slice. |
| `bricks/mcp/middleware_audit.py` | Per-MCP-HTTP-request audit → stdout JSON + Redis pub `nexus:audit:mcp` + Redis QPS counters | Closest existing surface; foundation extends this middleware to also emit `mcp_tool_call` activity events. Transport-level audit stays in place. |
| `/metrics` endpoint + `prometheus_client` patterns | Prom exposure, used by auth/sandbox/manifest | Reused — foundation registers new metrics at the same endpoint. |

The activity subsystem is therefore additive: it sits above existing infra, depends only on stdlib + `prometheus_client` + SQLite, and does not touch `event_bus` / `event_log` / `audit_node`.

## Design

### Architecture

```
        ┌──────────────────── emission callsites ──────────────────────┐
        │  SearchService.search                                         │
        │  FederatedSearch.search                                       │
        │  RebacChecker.check  (deny path)                              │
        │  ApprovalService.request_and_wait                             │
        │  MCPAuditLogMiddleware  (per request → mcp_tool_call)         │
        └──────────────┬────────────────────────────────────────────────┘
                       │  emit(kind, actor, subject, result, latency_ms, trace_id)
                       ▼
        ┌──────────────────────────────────────────────────────────────┐
        │  nexus.services.activity                                      │
        │  ┌────────────────┐    ┌──────────────────────┐              │
        │  │  Emitter       │───▶│  bounded asyncio.Queue│             │
        │  │  (singleton)   │    │  (default 10k)        │             │
        │  │  + drop_count  │    └──────────┬───────────┘              │
        │  │  + prom hooks  │               │ drain                     │
        │  └────────────────┘               ▼                            │
        │                          ┌──────────────────┐                 │
        │                          │  Worker          │                 │
        │                          │  drain→batch→    │                 │
        │                          │  sinks.write()   │                 │
        │                          └──────┬───────────┘                 │
        │                                 ▼                              │
        │                          ┌──────────────────┐                 │
        │                          │  SinkProtocol    │                 │
        │                          │  └─ SQLiteSink   │ ← default       │
        │                          │  └─ NoopSink     │ ← tests/disabled│
        │                          └──────────────────┘                 │
        │                                                                │
        │  + RetentionTask (periodic prune > N days)                     │
        │  + Prom Counters/Histograms updated at emit() callsites        │
        └────────────────────────────────────────────────────────────────┘

        Lifespan: setup_activity()/shutdown_activity() in
                  src/nexus/server/lifespan/observability.py
```

### Module layout

Under `src/nexus/services/activity/`:

| File | Purpose |
|---|---|
| `__init__.py` | Public API: `emit`, `get_emitter`, `set_emitter`, `ActivityEvent`, `EventKind`, `Result` |
| `events.py` | `ActivityEvent` dataclass, `Actor`, `Subject`, enums (`EventKind`, `Result`) |
| `emitter.py` | `Emitter` interface + `NoopEmitter`; `QueueEmitter` impl with bounded queue and drop counter |
| `worker.py` | `ActivityWorker` — drain queue, batch insert, pluggable sinks |
| `sinks/protocol.py` | `SinkProtocol`: `async write_batch(events) -> None`, `async close() -> None` |
| `sinks/sqlite.py` | `SQLiteSink` — schema bootstrap, batch insert, indexed |
| `sinks/noop.py` | `NoopSink` for tests / disabled mode |
| `retention.py` | `RetentionTask` — periodic prune by `ts < now - retention_days` + VACUUM |
| `metrics.py` | Prom Counter / Histogram / Gauge instances per the catalog below |
| `config.py` | Env-var parsing → `ActivityConfig` dataclass |
| `lifespan.py` | `setup_activity(config) -> Emitter`, `shutdown_activity()` |

Tests under `tests/unit/services/activity/` and `tests/integration/services/activity/` (see Testing).

### Public API

```python
from nexus.services.activity import emit, EventKind, Result

emit(
    kind=EventKind.SEARCH,
    actor_token_hash=token_hash,        # 16-char sha256 prefix; never raw token
    actor_agent="claude-myapp",
    actor_user="alice",
    subject_zone="eng",
    subject_extra={"query_len": 18, "hits": 12},
    result=Result.OK,
    latency_ms=42,
    trace_id="abc123",
)
```

`emit()` is non-async, never raises, never blocks. Internally:
- Builds `ActivityEvent` (ULID id, current UTC ts).
- Updates the matching Prom metric inline.
- Calls `queue.put_nowait(event)`; on `QueueFull`, increments `nexus_activity_drops_total` and returns.

Singleton resolution: module-level `_EMITTER` initialized to `NoopEmitter()`; `setup_activity()` swaps in a `QueueEmitter` at server startup. Tests use `set_emitter()` to install `NoopEmitter` (unit) or keep `QueueEmitter` and swap in a `RecordingSink` (integration).

`emit()` works without a running event loop (`queue.put_nowait` is synchronous on `asyncio.Queue`); the worker is the only component that requires the loop.

### Event schema

`ActivityEvent` (frozen dataclass):

| Field | Type | Notes |
|---|---|---|
| `id` | `str` | ULID, sortable, unique |
| `ts` | `str` | ISO-8601 UTC, microsecond precision |
| `kind` | `EventKind` | `SEARCH` \| `FETCH` \| `MCP_TOOL_CALL` \| `ZONE_ACCESS` \| `POLICY_BLOCK` \| `APPROVAL` |
| `result` | `Result` | `OK` \| `BLOCKED` \| `PENDING_APPROVAL` |
| `latency_ms` | `int \| None` | Elapsed since operation start |
| `trace_id` | `str \| None` | OTEL trace id when available |
| `actor_token_hash` | `str \| None` | sha256(raw_token)[:16] — matches `MCPAuditLogMiddleware` |
| `actor_agent` | `str \| None` | Agent name (e.g. `claude-myapp`) |
| `actor_user` | `str \| None` | Owner user id |
| `subject_zone` | `str \| None` | Target zone id |
| `subject_extra` | `dict \| None` | Per-kind: `doc_id`, `tool`, `query`, etc. |
| `meta` | `dict \| None` | Catch-all for forward-compat fields |

### SQLite schema

```sql
CREATE TABLE activity_events (
  id              TEXT PRIMARY KEY,        -- ULID
  ts              TEXT NOT NULL,           -- ISO-8601 UTC
  kind            TEXT NOT NULL,
  result          TEXT NOT NULL,
  latency_ms      INTEGER,
  trace_id        TEXT,
  actor_token_hash TEXT,
  actor_agent     TEXT,
  actor_user      TEXT,
  subject_zone    TEXT,
  subject_extra   TEXT,                    -- JSON
  meta            TEXT                     -- JSON
) STRICT;

CREATE INDEX idx_ae_ts          ON activity_events(ts);
CREATE INDEX idx_ae_kind_ts     ON activity_events(kind, ts);
CREATE INDEX idx_ae_token_ts    ON activity_events(actor_token_hash, ts);
CREATE INDEX idx_ae_zone_ts     ON activity_events(subject_zone, ts);
```

Open-time PRAGMAs: `journal_mode=WAL`, `synchronous=NORMAL`, `temp_store=MEMORY`, `busy_timeout=5000`.

Schema bootstrap is idempotent (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`). The activity DB is **separate** from the main Nexus DB — no alembic migration needed; lives at `$NEXUS_ACTIVITY_DB_PATH` (default `$NEXUS_DATA_DIR/activity.db`).

### Metrics catalog

Registered at module import (in `metrics.py`) on the global `prometheus_client.REGISTRY`:

| Metric | Type | Labels | Updated at |
|---|---|---|---|
| `nexus_search_requests_total` | Counter | `zone`, `token_hash`, `status` | `emit(kind=SEARCH, ...)` |
| `nexus_search_latency_seconds` | Histogram | `zone` | `emit(kind=SEARCH, ...)` (no phase label in foundation) |
| `nexus_mcp_tool_calls_total` | Counter | `tool`, `status` | `emit(kind=MCP_TOOL_CALL, ...)` |
| `nexus_policy_blocks_total` | Counter | `kind` | `emit(kind=ZONE_ACCESS\|POLICY_BLOCK, result=BLOCKED, ...)` |
| `nexus_approvals_pending` | Gauge | — | `+1` on `emit(kind=APPROVAL, result=PENDING_APPROVAL)`; `-1` when approval decided (also emitted) |

Plus internal subsystem metrics:

| Metric | Type | Purpose |
|---|---|---|
| `nexus_activity_drops_total` | Counter | Queue overflow drops; alertable |
| `nexus_activity_sink_errors_total{sink}` | Counter | Sink write failures |
| `nexus_activity_sink_status{sink, state}` | Gauge | 1 = healthy, 0 = degraded |
| `nexus_activity_retention_pruned_total` | Counter | Rows deleted by retention task |

Activity-disabled mode (`NEXUS_ACTIVITY_ENABLED=0`) installs `NoopEmitter`, which skips both queue insertion and metric updates — the catalog ships and unships as a unit.

### Emission callsites (foundation)

| Kind | File / function | Notes |
|---|---|---|
| `SEARCH` | `bricks/search/search_service.py:178` `SearchService.search()` and `bricks/search/federated_search.py:438` `FederatedSearch.search()` | Both paths emit; latency = wall time of the public method |
| `MCP_TOOL_CALL` | `bricks/mcp/middleware_audit.py` `MCPAuditLogMiddleware._record_from_scope` | Adds an `emit()` call alongside existing stdout/Redis records; transport-level audit unchanged |
| `ZONE_ACCESS` / `POLICY_BLOCK` | `bricks/rebac/enforcer.py:404` `RebacChecker.check()` deny path (and `_log_bypass_denied`) | Single emit at decision point covers both kinds; `kind` label distinguishes |
| `APPROVAL` | `bricks/approvals/service.py:211` `ApprovalService.request_and_wait()` (creation → `PENDING_APPROVAL`) and `:583` `decide()` (resolution → `OK`/`BLOCKED`) | Two emit points keep the gauge balanced |

`fetch` kind is intentionally not wired in foundation; MCP `read_file` calls show up as `mcp_tool_call` events. A dedicated kernel-read instrumentation lands with the audit slice.

### Configuration (env vars only in foundation, per Q12c)

| Env var | Default | Purpose |
|---|---|---|
| `NEXUS_ACTIVITY_ENABLED` | `1` | Master switch; `0` installs NoopEmitter |
| `NEXUS_ACTIVITY_DB_PATH` | `$NEXUS_DATA_DIR/activity.db` | SQLite file path |
| `NEXUS_ACTIVITY_RETENTION_DAYS` | `30` | Prune threshold; `0` disables retention task (unbounded growth) |
| `NEXUS_ACTIVITY_QUEUE_SIZE` | `10000` | Bounded queue capacity |
| `NEXUS_ACTIVITY_BATCH_SIZE` | `200` | Worker batch size |
| `NEXUS_ACTIVITY_BATCH_TIMEOUT_S` | `0.5` | Worker batch flush timeout |
| `NEXUS_ACTIVITY_DROP_ON_OVERFLOW` | `1` | If `0`, callers block on `put` (debug only — violates SLA) |

Multi-sink config (`[activity]` config-file block) lands with the OTEL/JSONL/webhook slice.

### Lifespan integration

Add an entry to `src/nexus/server/lifespan/observability.py`:

```python
("activity", "nexus.services.activity.lifespan", "setup_activity", "shutdown_activity"),
```

`setup_activity()`:
1. Read `ActivityConfig` from env.
2. If disabled → install `NoopEmitter`, return.
3. Open SQLite, bootstrap schema with PRAGMAs.
4. Construct `QueueEmitter`, `ActivityWorker(sinks=[SQLiteSink])`, `RetentionTask`.
5. Start worker + retention as supervised asyncio tasks (existing supervisor pattern).
6. `set_emitter(QueueEmitter(...))`.

`shutdown_activity()`:
1. `set_emitter(NoopEmitter())` (stop accepting new events).
2. Await worker drain with 5 s timeout.
3. Cancel retention task.
4. Close SQLite.

### Error handling

| Failure | Response | Visibility |
|---|---|---|
| Queue full | Drop event, `nexus_activity_drops_total += 1`. Never raise to caller. | Metric (alertable) |
| SQLite write fails | Log warn, `nexus_activity_sink_errors_total{sink="sqlite"} += 1`. Drop the batch (no retry queue — best-effort telemetry). | Metric + log |
| SQLite file missing/corrupt at startup | Log error, fall back to `NoopSink`. Server continues. | Log + `nexus_activity_sink_status` |
| Worker task crashes | Lifespan supervisor restarts task with backoff. | Log + restart counter |
| Retention task fails | Log warn, retry next tick. | Log |
| Activity disabled | `NoopEmitter` no-ops emit + metrics. | n/a |
| `emit()` from sync context (no loop) | `queue.put_nowait` is sync-safe; only worker needs the loop. | n/a |

### Performance budget

Hot-path target (issue SLA): no measurable impact on search.

`emit()` cost at SEARCH callsite:
1. ULID gen — ~500 ns
2. ActivityEvent dataclass build — ~1 µs
3. Prom Counter `.labels(...).inc()` — ~3 µs
4. Prom Histogram `.observe()` — ~3 µs
5. `queue.put_nowait(event)` — ~1 µs

Total: **<10 µs per emit** vs. typical search 10–100 ms ⇒ ≤0.1% overhead. CI bench gates regressions.

Backpressure stance: drop, never block. Telemetry must not stall the operation.

Crash safety: SQLite WAL fsyncs per commit; crash loses in-flight queued events only (≤10k = ≤MB-scale). Acceptable for telemetry.

## Testing

### Unit (`tests/unit/services/activity/`)

| File | Covers |
|---|---|
| `test_emitter.py` | Singleton get/set; NoopEmitter discards; QueueEmitter.put_nowait; drop counter on overflow; set_emitter swap; emit from sync context |
| `test_events.py` | ActivityEvent serialization roundtrip; enum values match issue schema; subject_extra arbitrary JSON |
| `test_worker.py` | Drain loop batches up to N; flushes on timeout below batch size; sink exception doesn't kill worker; shutdown drains queue then exits |
| `test_sqlite_sink.py` | Schema bootstrap idempotent; batch insert; STRICT-mode enforcement; PRAGMAs applied; corrupt-file fallback |
| `test_retention.py` | Deletes rows older than threshold; retains newer; VACUUM triggered above row threshold; no-op when retention=0 |
| `test_metrics.py` | Counters/Histogram/Gauge exposed; labels match catalog; `nexus_approvals_pending` inc/dec balanced |
| `test_config.py` | Env var parsing; defaults; invalid values rejected with clear error |

### Integration (`tests/integration/services/activity/`)

| Test | Covers |
|---|---|
| `test_emit_to_sqlite_e2e.py` | `setup_activity()` with tmp DB; emit 1000 events from multiple async tasks; await drain; query DB for counts/contents/indexes |
| `test_emission_callsites.py` | Wire `RecordingSink`; exercise SearchService.search, FederatedSearch.search, RebacChecker.check deny, ApprovalService.request_and_wait, MCPAuditLogMiddleware → assert expected event recorded |
| `test_lifespan_supervision.py` | Worker task killed externally; lifespan restarts it; new events still recorded |
| `test_metrics_endpoint.py` | After emits, scrape `/metrics`; assert all five catalog metrics present with expected labels |

### Perf bench (`benchmarks/activity/`)

| Bench | Target |
|---|---|
| `bench_emit.py` | `emit()` p50 < 10 µs / p99 < 50 µs |
| `bench_search_with_activity.py` | SearchService.search with activity ON vs OFF — overhead < 1 % on p50 |

### Test doubles

`RecordingSink` (in-memory list), `FlakySink` (raises N % of writes), `SlowSink` (sleeps in `write_batch` — exercises queue overflow path).

## Acceptance criteria (foundation slice)

- [ ] `nexus.services.activity` package exists with public API per spec; `emit()` callable from any service; `NoopEmitter` is the default.
- [ ] `ActivityEvent` schema covers all six event kinds with the fields above.
- [ ] `SQLiteSink` durably persists batched events to `$NEXUS_ACTIVITY_DB_PATH` with the indexed schema.
- [ ] `RetentionTask` prunes rows older than `NEXUS_ACTIVITY_RETENTION_DAYS`.
- [ ] All five issue-named metrics exposed at `/metrics` with labels per the catalog.
- [ ] Five emission callsites wired (SearchService, FederatedSearch, RebacChecker deny, ApprovalService create+decide, MCPAuditLogMiddleware).
- [ ] `emit()` p50 < 10 µs / p99 < 50 µs (CI bench enforced).
- [ ] No measurable impact on SearchService.search p50 (CI bench enforced).
- [ ] `NEXUS_ACTIVITY_ENABLED=0` installs NoopEmitter cleanly with no leftover prom registrations.
- [ ] Lifespan supervisor restarts the worker on crash.

## Out-of-slice (tracked for follow-ups)

| Item | Slice |
|---|---|
| `nexus term` operator TUI | Slice 2 (consumer of activity sink) |
| `nexus logs --follow [--sandbox]` | Slice 2 |
| `nexus stats` summary | Slice 2 |
| Hash-chained tamper-evident audit + `nexus audit export --from --to` | Slice 3 (independent; builds on `audit_node` + this sink) |
| `fetch` kind on kernel read path | Slice 3 |
| Phase-labelled `nexus_search_latency_seconds{phase}` | Slice 4 |
| OTEL / JSONL / webhook sinks + multi-sink config block | Slice 5 |
