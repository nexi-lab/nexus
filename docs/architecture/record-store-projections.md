# RecordStore Projections: write-through, fence, reconcile

Issue: nexi-lab/nexus#4738 (fenced projections). Companions: #4736 (write →
searchable, `index_seq`), #4737 (read-your-writes revision token), #4741
(conformance suite). Tenant tracking: DeepBuildAI/koodle#3079.

The kernel (`nexus-vfs`, raft + CAS) is the authority for file content and
metadata. The RecordStore (SQLite or PostgreSQL) holds **projections** of it
that services query: `file_paths` (the search zone join, `list_versions`
lookup), `version_history` (`list_versions`, `get_version`, rollback),
`operation_log` (audit, `/api/v2/operations`, `/api/v2/ops/replay`, event
delivery) and `metadata_change_log` (MCL / aspects). This document is the
contract for how those projections follow the kernel.

## 1. What changed

Before #4738 the projection was fed by a 200 ms debounce over an in-memory
`deque(maxlen=10_000)` (#809): `list_versions` right after a write could be
empty, a burst over 10 k events silently dropped the oldest (only a counter
moved), a crash lost everything in flight, and readers had to call the
admin-only `flush_write_observer` RPC.

Now (`src/nexus/storage/piped_record_store_write_observer.py`):

| Property | Contract |
|----------|----------|
| Visibility | `operation_log`, `file_paths`, `version_history` rows are committed **before the write returns**. `list_versions` immediately after a write returns the new version with no flush call. |
| Fence | Every write returns `projection_seq` — the `operation_log.sequence_number` of its row. `GET /api/v2/operations/wait?seq=N` blocks until that row is committed (200) or the timeout passes (412). |
| Throughput | Concurrent writers share one SQL transaction (group commit); one committer thread allocates sequence numbers, so no MAX+1 retry storms. |
| Failure | `AuditConfig.strict_mode=True` (default): commit failure after retries or no confirmation within `NEXUS_PROJECTION_TIMEOUT_S` (5 s) raises `AuditLogError` from the write. `strict_mode=False`: the write succeeds with `projection_seq: null`, the event stays queued for retry, CRITICAL log. |
| Never silent | Both queues are bounded. The deferred queue backpressures the committer for 2 s before dropping; every drop is an ERROR log plus `nexus_projection_events_dropped_total{queue}`. |
| Deferred | Only MCL rows and post-flush hooks (extraction, lineage) run off the write path, on a dedicated thread. |
| Recovery | `POST /api/v2/admin/reconcile-projections` repairs the projection from the kernel after a crash, an outage or a drop. |

## 2. Data flow

```
writer thread                              projection-commit thread
─────────────                              ────────────────────────
kernel sys_write (raft commit, durable)
post-hook → SyncAuditWriteInterceptor
  observer.on_write(...)
    ticket = _submit(events)  ───────────▶ drain ≤ 100 events (all waiting tickets)
    ticket.done.wait(timeout)              ONE transaction:
                                             OperationLogger.log_operation → sequence_number
                                             VersionRecorder.record_write  → file_paths, version_history
                                           ticket.seq = sequence_number; wake writers
    ctx.extra["projection_seq"] = seq      hand events to projection-deferred thread
NexusFS.write returns {..., projection_seq}            │
                                                        ▼
                                           MCLRecorder rows, extraction + lineage hooks
```

`version_history.extra_metadata` records the kernel `gen` of the write
(`{"gen": N}`) so reconcile can tell a lagging kernel view from a lost row.

## 3. Surfaces

### Writes return `projection_seq`

| Surface | Field |
|---------|-------|
| `POST /api/v2/files/write` | `projection_seq` |
| `POST /api/v2/files/batch/write` | `results[].projection_seq` |
| `DELETE /api/v2/files/delete`, `POST /api/v2/files/rename`, `POST /api/v2/files/mkdir` | `projection_seq` |
| `POST /api/v2/files/rename-batch` | `results[].projection_seq` |
| `NexusFS.write` / `sys_write` / `write_batch` / `sys_unlink` / `rmdir` / `sys_rename` / `mkdir` / `rename_batch` | `"projection_seq"` key in the returned dict (`mkdir` used to return `None`) |
| `POST /api/nfs/{method}` and gRPC `Call` (`write`, `delete`, `rename`, `mkdir`, `rmdir`) | `projection_seq` next to the legacy shape (`{"deleted": true, ...}`), present only when confirmed |

`null` means the projection was not confirmed: the legacy synchronous
observer is in use (`NEXUS_ENABLE_WRITE_BUFFER=false` — it commits inline but
returns no sequence), or a non-strict observer timed out. Rename returns the
sequence of its upsert row (the second of the two audit rows it writes).

CLI: `nexus ops wait --seq N [--timeout-ms MS]` (exit 0 applied, 1 not yet)
and `nexus admin fs reconcile-projections --prefix P [--dry-run]
[--retire-missing] [--max-entries N]` (runs locally when the CLI holds the
RecordStore, otherwise through the admin REST route).

### `GET /api/v2/operations/wait`

```
GET /api/v2/operations/wait?seq=1234&timeout_ms=5000
```

| Outcome | Status | Body |
|---------|--------|------|
| Row `seq` committed in the caller's zone | 200 | `{seq, applied: true, latest_seq, zone_id, waited_ms}` |
| Not committed before `timeout_ms` (default 5 000, max 30 000, `0` probes once) | **412** | `detail = {error: "projection_not_applied", seq, latest_seq, zone_id, waited_ms}` |

Scoped to the token's zone: a sequence that belongs to another zone looks
exactly like one that has not landed. Each probe opens a fresh RecordStore
session so it sees rows committed by other connections (SQLite pins a read
snapshot per transaction). `latest_seq` is the zone's newest committed
sequence — the same number `/api/v2/ops/replay?from_sequence=` consumes.

With write-through the sequence is normally committed when the write
returns, so on the same node the wait answers immediately. It matters when
(a) the observer runs non-strict and timed out (`projection_seq` was `null`
— nothing to wait on; poll `latest_seq` or reconcile), (b) another node
shares the same PostgreSQL and the caller's sequence came from elsewhere,
and (c) as the protocol replacement for `flush_write_observer`, which is
admin-only and now only drains deferred work.

### `POST /api/v2/admin/reconcile-projections`

```json
{"prefix": "/workspace", "dry_run": false, "retire_missing": false, "max_entries": null}
```

Admin only. Walks the kernel under `prefix` (`sys_readdir` recursive with
details, under the caller's context) and, per regular file with content:

| Verdict | Condition | Action |
|---------|-----------|--------|
| `created` | no active `file_paths` row | `file_paths` + `version_history` v1 + `operation_log` write row, `agent_id = system:projection-reconcile` |
| `in_sync` | head row's recorded `gen` equals the kernel's (legacy rows without a `gen`: head `content_id` equals the kernel's) | none |
| `repaired` | kernel `gen` newer than the head row's (legacy rows: content differs) | new `version_history` row for the kernel's current content, `file_paths` updated, `operation_log` write row |
| `stale_kernel` | head row records a `gen` newer than the kernel's | none — this node's kernel view is behind (follower lag); the projection is not the stale side |
| `retired` (`retire_missing=true` only) | active row under `prefix` the kernel no longer lists | soft delete + `operation_log` delete row |

The comparison keys on the kernel `gen` rather than `content_id` because a
path-addressed backend (the local backend in a single-node deployment)
reports the storage key — the path — as `content_id`, which never changes
across writes; `gen` increments on every write everywhere. The observer
keys `file_paths` rows by the *writer's* zone, and `list_versions` looks a
path up by `virtual_path` alone, so reconcile matches an existing row in
**any** zone (a root admin never duplicates rows a `research` token wrote),
repairs in that row's zone, and uses the caller's zone only for rows that
do not exist anywhere. Intermediate versions lost in a crash window cannot be
recovered (the kernel keeps only current metadata); the repair records the
current content as the next version. `retire_missing` is opt-in because a
partial kernel listing (an unrouted mount) would otherwise retire live
rows; it is skipped when `max_entries` truncated the walk.

## 4. Operations

Metrics (Prometheus, `/metrics`):

| Metric | Meaning |
|--------|---------|
| `nexus_projection_events_dropped_total{queue="pending"\|"deferred"}` | Events shed from a bounded queue. `pending` drops lose audit/version rows → run reconcile. `deferred` drops lose MCL rows + hooks → run `POST /api/v2/admin/reindex`. Non-zero is an incident. |
| `nexus_projection_events_failed_total` | Rows whose commit failed after retries (poison rows, #4645); their writers got `AuditLogError` in strict mode. |
| `nexus_projection_write_through_timeouts_total` | Writes that returned before their row was confirmed (strict: as an error; non-strict: with `projection_seq: null`). |
| `nexus_projection_pending_events{queue}` | Current queue depth. |

The observer's `metrics` property carries the same counters plus
`applied_seq` (highest sequence this process committed).

Configuration:

| Setting | Default | Effect |
|---------|---------|--------|
| `AuditConfig.strict_mode` / `audit_strict_mode` | `true` | Fail the write (`AuditLogError`) when its projection is not confirmed. |
| `NEXUS_PROJECTION_TIMEOUT_S` | `5.0` | How long a write waits for its commit before the strict/non-strict policy applies. |
| `NEXUS_PROJECTION_MODE` | `write_through` | `async` returns from the write as soon as the event is queued (`projection_seq: null`, no wait). Everything else in this document — group commit thread, bounded queues, retry/salvage, metrics, reconcile — is unchanged. Unknown values log a warning and behave as `write_through`. |
| `NEXUS_ENABLE_WRITE_BUFFER` | `true` | `false` selects the legacy fully synchronous observer (inline MCL, `/__sys__/versioning` snapshots, no `projection_seq`). |

### Async mode (`NEXUS_PROJECTION_MODE=async`)

The write-through wait costs one projection transaction per write on the
request path: `SELECT max(sequence_number)+1`, the `operation_log` insert,
the `file_paths` lookup + tombstone delete + insert (or update) and the
`version_history` insert, then the commit's fsync — about ten round-trips.
Measured on a remote Postgres in the same region this added 80–100 ms to a
write that previously spent ~25 ms server-side. Tenants that keep their own
version history and never fence on `projection_seq` or `/operations/wait`
can opt out: the writer submits its ticket and returns `projection_seq: null`
immediately, and the commit thread coalesces the tickets that arrive within
`debounce_seconds` (0.2 s, the pre-#4738 window) into one transaction before
landing the rows exactly as before — a burst costs one session, one ORM flush
and one fsync instead of one per write, which also keeps that CPU from
competing with request threads for the GIL right after every write. What
changes: `list_versions` right after a write may briefly miss the new row
(the old #809 behaviour), a strict-mode commit failure can no longer be
reported to the writer (it is still logged at ERROR and counted in
`nexus_projection_events_failed_total`), and the crash window grows from
"rows in flight" to "rows queued" — still bounded by the pending queue and
still repairable with `POST /api/v2/admin/reconcile-projections`.

Crash window: the kernel commit precedes the projection commit, so a
`kill -9` between them leaves the kernel ahead by the rows in flight (at
most one per writer thread; those writers never received a response). After
restart, `POST /api/v2/admin/reconcile-projections` on the affected prefix
restores `file_paths` / `version_history` from the kernel's current state;
`dry_run: true` first shows the drift. Tenants that fenced on
`projection_seq` are unaffected: the fence only reports sequences whose row
is already committed.

`tests/e2e/self_contained/test_projection_crash_recovery_e2e.py` is the
acceptance test for this: a real `nexusd` + kernel, a 20 000-write burst from
8 threads, `SIGKILL` of the whole process group at 40 %, restart on the same
data directory, reconcile, then a path-by-path comparison of `file_paths`,
`version_history` and `operation_log` against the kernel listing. A
representative run: 8 000 writes acknowledged before the kill, 8 001 files in
the kernel afterwards (one write committed by the kernel but never
acknowledged — the crash window), reconcile `created: 1`, zero mismatches.

## 5. Tenant guidance

1. Keep `projection_seq` from every write next to `content_id` and the
   `revision` token (#4737). `revision` fences kernel reads (`read`, `list`,
   `search`); `projection_seq` fences the SQL-backed history and audit
   surfaces (`list_versions`, `/api/v2/operations`, `/api/v2/ops/replay`).
2. On the node that served the write no wait is needed. Across nodes sharing
   a RecordStore, `GET /api/v2/operations/wait?seq=` before the history call.
3. `projection_seq: null` means the deployment runs non-strict and the row is
   pending; treat history as eventually consistent for that write and expect
   `nexus_projection_write_through_timeouts_total` to have moved.
4. After a server crash, ask an operator to run reconcile (or run it with an
   admin token) before trusting `list_versions` for paths written in the last
   seconds before the crash.
