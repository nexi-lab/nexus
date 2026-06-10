# Activity store operations

The activity subsystem records telemetry events (search, MCP tool calls,
policy blocks, approvals) to SQLite. Since issue #4336 it writes per-UTC-day
segment files instead of one ever-growing `activity.db`:

```
$NEXUS_ACTIVITY_DIR/activity-2026-06-09.db   (+ -wal / -shm siblings)
```

Retention deletes whole expired segment files — O(1) disk reclaim, no VACUUM.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `NEXUS_ACTIVITY_ENABLED` | `1` | Master switch for the subsystem. |
| `NEXUS_ACTIVITY_DIR` | `<dir of NEXUS_ACTIVITY_DB_PATH>/activity` | Segment directory. Point at a dedicated volume to isolate telemetry I/O and capacity from user payload data. |
| `NEXUS_ACTIVITY_DB_PATH` | `$NEXUS_DATA_DIR/activity.db` | Legacy pre-segment db location; only used to find and clean up that file. Also anchors the `NEXUS_ACTIVITY_DIR` default. |
| `NEXUS_ACTIVITY_RETENTION_DAYS` | `30` | Segments older than this are deleted (whole files). `0` disables retention. Granularity is one day: a segment is removed once its newest possible row passes the cutoff, so worst-case over-retention is <24h. |
| `NEXUS_ACTIVITY_MIN_FREE_MB` | `1024` | Disk-pressure floor: when free space on the segment volume drops below this, the sink **sheds** activity batches (counted, alerted) instead of consuming the space user file writes need. `0` disables shedding. |
| `NEXUS_ACTIVITY_SAMPLE_RATE` | `1.0` | Global keep-probability for `result=ok` events. |
| `NEXUS_ACTIVITY_SAMPLE_RATES` | unset | Per-kind overrides, e.g. `search=0.05,mcp_tool_call=0.2`. Sampleable kinds: `search`, `fetch`, `mcp_tool_call`, `op`, `exec`. Audit kinds (`approval`, `policy_block`, `zone_access`) are always recorded — configuring a rate for them is a startup error. |
| `NEXUS_ACTIVITY_QUEUE_SIZE` / `..._BATCH_SIZE` / `..._BATCH_TIMEOUT_S` | `10000` / `200` / `0.5` | In-process queue/batching (unchanged by #4336). |

Sampling never drops non-`ok` results, nor any event of an audit kind
(`approval`, `policy_block`, `zone_access`) — an approved approval is
`result=ok` but is still audit data. Prometheus counters are recorded
before sampling, so `/metrics` stays exact regardless of the rate.

## Sizing

Steady-state disk ≈ `retention_days × daily event volume × ~400 B/event`,
plus a ≤64 MB WAL on the active segment (`journal_size_limit`, truncate
checkpoints every 5 min). At 12 M events/day and 30-day retention that is
~150 GB — set `NEXUS_ACTIVITY_SAMPLE_RATES` and/or lower
`NEXUS_ACTIVITY_RETENTION_DAYS` to fit your volume. Shedding is the
backstop, not the sizing mechanism.

## Monitoring

| Metric | Alert on |
|---|---|
| `nexus_activity_shed_total` | any sustained increase — telemetry is being dropped to protect user writes |
| `nexus_activity_disk_free_bytes` | approaching `NEXUS_ACTIVITY_MIN_FREE_MB` |
| `nexus_activity_store_bytes` | unexpected growth vs. your sizing budget (updated by retention sweeps; static when retention is disabled) |
| `nexus_activity_segments_deleted_total` | flatlining at 0 with non-zero retention (sweep not running) |
| `nexus_activity_sink_errors_total` | write/open failures |

Shedding transitions also emit edge-triggered `ERROR`/`INFO` log lines
(`activity disk pressure ...`).

## Upgrading from the single-file store

The old `activity.db` is frozen on first boot after the upgrade (no new
writes) and deleted automatically once its newest row is older than
`NEXUS_ACTIVITY_RETENTION_DAYS` — full reclaim within one retention window,
with no VACUUM and no DELETE churn. To reclaim immediately instead, delete
`activity.db`, `activity.db-wal`, and `activity.db-shm` yourself; the
server never reopens them.
