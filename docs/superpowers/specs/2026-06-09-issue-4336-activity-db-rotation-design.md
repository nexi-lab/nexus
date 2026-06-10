# Activity store: segment rotation, disk-pressure shedding, sampling (issue #4336)

**Issue:** [#4336](https://github.com/nexi-lab/nexus/issues/4336) — activity.db unbounded effective growth (VACUUM impossible >50% disk, WAL starved) fills data volume → global write outage.

**Incident summary:** production filled a 184 GB volume with a 146 GB `activity.db` + 50 GB WAL at ~12 M events/day. Hourly retention DELETE ran, but SQLite never returns disk without VACUUM, VACUUM needs ~db-size free space (impossible past ~50% disk), and the steady writer starved WAL checkpoints. User `files/write` then failed with `StorageFull`.

## Goals

1. Disk usage of the activity store is bounded and reclaimable in O(1) (no VACUUM, no large DELETEs).
2. WAL size is capped under a sustained writer.
3. Under disk pressure, activity events are shed before user file writes fail.
4. Event volume is controllable via sampling (12 M/day for one tenant is pathological).
5. Degradation is observable: Prometheus metrics + ERROR-level logs.
6. Existing oversized deployments converge to the bounded state without operator surgery.

## Non-goals (follow-up issues)

- External alert routing (webhook/pager) for `should_alert` events — issue ask #7 beyond logs/metrics is an observability-infra concern.
- Event aggregation/rollups (ask #4 mentions aggregation; only sampling ships here).
- A query/read API over activity events (store is write-only in-server today; only `retention.py` touches it).
- Changing the `files/write` error path — fixed at the source instead.

## Design

### 1. Per-day segment rotation (replaces single activity.db)

`SQLiteSink` writes to per-UTC-day segment files in a segment directory:

```
$NEXUS_ACTIVITY_DIR/activity-2026-06-09.db   (+ -wal/-shm siblings)
```

- **Segment selection:** at flush time (not per-event), the sink compares the current UTC date to the open segment's date. On mismatch it closes the old connection (final `wal_checkpoint(TRUNCATE)` happens via close) and opens/creates the new segment with the same idempotent schema + indexes + PRAGMAs. All rollover work happens on the existing single-writer executor thread — no new locking.
- **Clock injection:** the sink takes a `now_fn` (default `datetime.now(UTC)`) so midnight rollover is unit-testable.
- **Schema/PRAGMAs per segment:** existing four PRAGMAs plus `PRAGMA journal_size_limit = 67108864` (64 MB). No `auto_vacuum`: segments are append-only and reclaimed by unlink, never DELETE, so incremental vacuum has nothing to do (issue ask #3 is superseded; activating `auto_vacuum` on an existing large db requires the very VACUUM that is impossible — documented in the issue close-out).
- **Periodic checkpoint:** every 300 s (constant), the sink runs `PRAGMA wal_checkpoint(TRUNCATE)` on the executor thread between batches. Single writer + no in-process readers means the checkpoint generally wins; failures are non-fatal and logged at debug. Combined with `journal_size_limit`, the WAL can no longer grow to 50 GB intra-day (ask #2; the "brief writer pause" is free — the checkpoint runs serialized on the writer's own thread).
- **Crash safety:** unchanged — `INSERT OR IGNORE`, idempotent bootstrap, reopen-safe.

Bound: disk ≈ `retention_days × daily-volume` + one WAL cap. Rotation fixes *reclaim*; sampling (below) fixes *volume*.

### 2. Retention: unlink expired segments (no DELETE, no VACUUM)

`prune_older_than` is replaced by segment-based retention in `retention.py`:

- Compute `cutoff_ts = now(UTC) - retention_days`. Unlink every segment (plus `-wal`/`-shm`) whose date `< cutoff_ts.date()` — at that point every row in it is older than `cutoff_ts`. Worst-case over-retention is <24 h (today's behavior was exact-to-the-hour; acceptable coarsening, noted in docs).
- `retention_days <= 0` still disables retention entirely (explicit operator choice; shedding still protects the volume).
- `RetentionTask` keeps its hourly cadence, executor-thread execution, and stop semantics. It additionally updates the store-size gauge each tick (the disk-free gauge is maintained by the sink's cached probe).
- The active (today's) segment is never a deletion candidate by construction.

### 3. Legacy activity.db: freeze + auto-unlink

On the first boot with rotation, the old single `activity.db` (at `NEXUS_ACTIVITY_DB_PATH` / `$NEXUS_DATA_DIR/activity.db`) stops receiving writes — it is frozen. Each retention tick:

- If the legacy file exists, open read-only and query `SELECT max(ts) FROM activity_events` (indexed, cheap).
- If the table is missing/empty or `max(ts) < cutoff_ts`: unlink `activity.db`, `-wal`, `-shm`. Full O(1) reclaim within ≤ `retention_days` of upgrading — same data-loss semantics as the retention DELETE that already governs this data.
- Otherwise leave it alone (no DELETE churn, no WAL growth on a huge file). Errors (locked/corrupt) log a warning and skip until the next tick.
- Ops docs gain a note: operators needing immediate reclaim may stop the server (or rely on the frozen guarantee) and delete the legacy files manually.

### 4. Disk-pressure shedding

`SQLiteSink.write_batch` checks free space before writing:

- `shutil.disk_usage(segment_dir).free`, cached for 10 s (one statvfs per ~10 s, not per batch; `disk_usage` injectable for tests).
- If free < `NEXUS_ACTIVITY_MIN_FREE_MB` (default **1024**; `0` disables): drop the batch, increment `nexus_activity_shed_total` by the batch size, and emit an **edge-triggered** `logger.error` on entering shedding (plus `logger.info` on recovery). Telemetry dies before user writes do (ask #5).
- Segment creation and rollover are also guarded: if the open/create fails under a full disk, the sink degrades to dropping (counted) rather than crashing the worker.

### 5. Sampling (per-kind, audit-exempt)

In `QueueEmitter.emit`, before the capacity gate:

- Effective rate = `NEXUS_ACTIVITY_SAMPLE_RATES[kind]` if set, else `NEXUS_ACTIVITY_SAMPLE_RATE` (default 1.0). Keep iff `random.random() < rate`.
- **Exemption:** events with `result != OK` (`BLOCKED`, `PENDING_APPROVAL`) are never sampled out, and neither is any event of an audit-sensitive kind (`APPROVAL`, `POLICY_BLOCK`, `ZONE_ACCESS`) regardless of result — an approved approval carries `OK` but is audit data. Config rejects per-kind rates for audit kinds.
- Sampled-out events still call `record_metrics(...)` so Prometheus counters/histograms remain exact; only the durable SQLite row is skipped. They increment `nexus_activity_sampled_out_total`, **not** `ACTIVITY_DROPS` (intentional vs. failure).

### 6. Configuration

`ActivityConfig` additions (all env-driven, validated in `__post_init__`):

| Env var | Default | Meaning |
|---|---|---|
| `NEXUS_ACTIVITY_DIR` | `dirname(db_path)/activity` (i.e. `$NEXUS_DATA_DIR/activity`) | Segment directory. Mounting it on a separate volume satisfies ask #6. |
| `NEXUS_ACTIVITY_MIN_FREE_MB` | `1024` | Shedding threshold; `0` disables. Must be `>= 0`. |
| `NEXUS_ACTIVITY_SAMPLE_RATE` | `1.0` | Global keep-probability, `0.0–1.0`. |
| `NEXUS_ACTIVITY_SAMPLE_RATES` | unset | Per-kind overrides, e.g. `search=0.05,mcp_tool_call=0.2`. Keys must be `EventKind` values; values `0.0–1.0`. |

`NEXUS_ACTIVITY_DB_PATH` remains, repurposed: locates the legacy file for cleanup and anchors the default segment dir (so an operator who pointed it at `/mnt/telemetry/activity.db` gets segments in `/mnt/telemetry/activity/`). Existing vars (`ENABLED`, `RETENTION_DAYS`, `QUEUE_SIZE`, `BATCH_SIZE`, `BATCH_TIMEOUT_S`) unchanged.

### 7. Metrics & alerting surface (ask #7, in-repo scope)

New in `metrics.py`:

- `nexus_activity_shed_total` (Counter) — events dropped under disk pressure.
- `nexus_activity_sampled_out_total` (Counter) — events skipped by sampling.
- `nexus_activity_segments_deleted_total` (Counter) — segments (and the legacy db) unlinked by retention.
- `nexus_activity_disk_free_bytes` (Gauge) — last observed free space on the segment volume.
- `nexus_activity_store_bytes` (Gauge) — total bytes of segments + WALs (+ legacy file while present), updated each retention tick.

`ACTIVITY_RETENTION_PRUNED` stays registered (dashboards) but is documented as superseded. Edge-triggered ERROR logs on shed-enter and on legacy/segment unlink failure give the log pipeline an alertable event; wiring `should_alert` to a real notification channel is explicitly a follow-up.

## Component changes

| File | Change |
|---|---|
| `src/nexus/services/activity/sinks/sqlite.py` | Rotation (dir + segment naming + rollover), `journal_size_limit`, periodic TRUNCATE checkpoint, shedding check, injectable `now_fn`/`disk_usage`. |
| `src/nexus/services/activity/retention.py` | Segment-unlink retention; legacy freeze/auto-unlink; size gauges. `prune_older_than` removed (no external callers). |
| `src/nexus/services/activity/config.py` | New fields + parsing + validation above. |
| `src/nexus/services/activity/emitter.py` | Sampling gate in `QueueEmitter.emit`. |
| `src/nexus/services/activity/metrics.py` | New counters/gauges. |
| `src/nexus/services/activity/lifespan.py` | Pass segment dir + legacy path + sampling config through. |
| `docs/agents/self-observability.md` (+ env-var docs) | New knobs, legacy-cleanup ops note, retention-granularity note. |
| `tests/unit/services/activity/*`, `tests/integration/services/activity/*` | See below. |

Queue, contracts API, and all 13 `emit()` call sites are untouched. The worker gains one idle-tick hook: when a batch-collection window closes empty, it calls the optional `maintain()` method on each sink (duck-typed, not part of `SinkProtocol`) so the SQLite sink can roll a stale day's segment and release its handle — without this, a fully idle process would pin an unlinked expired segment's disk blocks indefinitely. `setup_activity` also runs one retention sweep BEFORE constructing the sink, so a boot on a full volume reclaims expired data first instead of degrading into the permanent NoopSink fallback. Legacy deletion executes under `BEGIN IMMEDIATE` on the legacy db, fencing a still-running pre-segment writer (rolling upgrade): a held lock reads as "active writer — keep"; a probe under the lock sees every committed row before the unlink.

## Error handling

- Sink write failure: unchanged path (warning + `ACTIVITY_SINK_ERRORS`), now also distinguishes shed-drops (counted separately, no error spam — edge-triggered).
- Rollover/segment-open failure: the batch is dropped and counted (`ACTIVITY_SINK_ERRORS` via the worker's per-batch warning); the next flush retries the open. The expected full-disk path is covered by shedding's edge-triggered ERROR before an open can fail.
- Retention unlink failure: warning, retried next tick. Legacy max(ts) query failure: warning, skip.
- Disk checks never raise into the worker; all failures degrade to "keep serving requests, lose telemetry" — the explicit priority from the incident.

## Testing

Unit:
- Rotation: same-day reuse, midnight rollover (injected clock), reopen-idempotent schema, `journal_size_limit` PRAGMA applied, checkpoint cadence.
- Retention: expired segments unlinked (+wal/+shm), active segment kept, `retention_days=0` no-op, legacy unlink on `max(ts) < cutoff`, legacy kept when fresh rows remain, corrupt legacy skipped with warning.
- Shedding: below-threshold drop + counter + edge-triggered log, recovery, `0` disables, cache honored (injected `disk_usage`).
- Sampling: rate honored (seeded RNG), per-kind override beats global, non-OK exempt, metrics still recorded, config validation errors.
- Config: new env parsing, defaults, bounds.

Integration:
- `test_emit_to_sqlite_e2e.py` updated: emit → drain → rows land in today's segment file.
- New: stale legacy db at boot unlinked by the first sweep; fresh legacy db stays frozen (no new rows, not deleted); cross-date segment split is covered at unit level with an injected clock.

Existing `test_sqlite_sink.py` / `test_retention.py` assertions updated for the new layout.

## Issue asks → coverage

| Ask | Status |
|---|---|
| 1. Size-capped rotation | ✅ per-day segments, unlink reclaim |
| 2. journal_size_limit + TRUNCATE checkpoints | ✅ per segment, serialized on writer thread |
| 3. auto_vacuum=INCREMENTAL for existing dbs | ⛔ superseded — technically impossible on the motivating 146 GB db (needs full VACUUM); legacy freeze+unlink achieves the reclaim instead |
| 4. Sampling/aggregation | ✅ sampling (per-kind, audit-exempt); aggregation deferred |
| 5. Disk-pressure shedding | ✅ min-free threshold, shed before user writes fail |
| 6. Separate path/volume option | ✅ `NEXUS_ACTIVITY_DIR` (+ existing `NEXUS_ACTIVITY_DB_PATH` anchoring) |
| 7. Surface should_alert | ◐ metrics + edge-triggered ERROR logs; external routing = follow-up issue |
