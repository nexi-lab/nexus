# Tiger Cache Boot Behavior

Issue #4342 bounds the Tiger Cache resource-map sync at boot. Before the fix,
`TigerCacheManager.initialize()` ran the sync synchronously on the boot thread
with no deadline — a wedged metastore/DB call blocked instead of raising, so
nexusd never bound its port and never logged again after the WIRED tier began.

## What runs at boot

When `NEXUS_ENABLE_TIGER_CACHE=true` (and the record store is PostgreSQL),
the WIRED tier starts a `tiger-init-sync` daemon thread that:

1. Syncs `tiger_resource_map` from existing metadata (full recursive listing,
   one upsert per path), gated by `NEXUS_SYNC_TIGER_RESOURCE_MAP`.
2. Optionally warms the Tiger Cache (`NEXUS_WARM_TIGER_CACHE`).

Boot waits for that thread at most `NEXUS_TIGER_INIT_TIMEOUT_SECONDS`, then
proceeds. On a healthy database the sync finishes inside the window and the
log order is unchanged:

```
Synced 155 resources to Tiger resource map
[BOOT:WIRED] 12/13 services ready (0.904s)
```

If the sync is still running when the window closes, boot continues and logs:

```
[WARNING] Tiger resource map sync still running after 30s; continuing boot
without it — permission checks use the slow path until the background sync
completes (Issue #4342)
```

Permission checks fall back to the slow path (same behavior as
`NEXUS_DISABLE_PERF_OPTIMIZATIONS=true`) until the background sync completes;
when the database unwedges, the sync finishes on its own — no restart needed.

## Environment knobs

| Variable | Default | Effect |
|----------|---------|--------|
| `NEXUS_ENABLE_TIGER_CACHE` | `false` in the stack template, `true` in-code | Construct the Tiger Cache at all (requires PostgreSQL) |
| `NEXUS_TIGER_INIT_TIMEOUT_SECONDS` | `30` | Max time boot waits for the resource-map sync; `0` = don't wait (pure background) |
| `NEXUS_SYNC_TIGER_RESOURCE_MAP` | `true` | Run the resource-map sync at boot |
| `NEXUS_WARM_TIGER_CACHE` | `false` | Also warm the cache during the sync thread |
| `NEXUS_DISABLE_PERF_OPTIMIZATIONS` | `false` | Skip sync, warm, and worker entirely (permanent slow path) |
| `NEXUS_ENABLE_TIGER_WORKER` | `false` | Start the legacy queue-processing worker |

## Troubleshooting

- **Boot warns "sync still running"** on every start: the metastore listing or
  the per-path upserts are slow or blocked. Check PostgreSQL for lock waiters
  on `tiger_resource_map` and for dead client connections. The server is fully
  functional meanwhile; only permission-check latency is degraded.
- **Symptom of the original bug** (pre-fix images): silence after the
  ShareLinkService init line, port never bound. Workaround on old images:
  `NEXUS_DISABLE_PERF_OPTIMIZATIONS=true`.
- The `tiger-init-sync` thread is named, so it is visible in
  `PYTHONFAULTHANDLER=1` dumps if you need to see where it is blocked.
- **Shutdown caveat**: graceful stop signals the sync between per-path
  upserts, but the initial `sys_readdir` listing is materialized in one call
  (Rust-side, see Issue #3706) and a single wedged DB statement cannot be
  interrupted from Python. In that case shutdown logs
  `tiger-init-sync still running ... abandoning daemon thread` and proceeds;
  the daemon thread cannot block process exit.
