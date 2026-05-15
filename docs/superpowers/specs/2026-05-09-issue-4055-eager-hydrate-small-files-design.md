# Issue #4055 - Eager-Hydrate Small Files Into L1 Cache Design

**Date**: 2026-05-09
**Issue**: [#4055](https://github.com/nexi-lab/nexus/issues/4055) - [P0] Eager-hydrate small files (<128KB) into L1 cache on workspace attach

## Context

`LocalDiskCache` (now backed by foyer per issue #4053) is reactive: entries populate only on first read. Cold-start latency for workspaces full of small configs/snippets/manifests scales as backend RTT × N files. Agent warmup workloads do many small reads, so the linear penalty hurts.

AWS S3 Files solves this by eagerly hydrating small files (<128KB) on mount and leaving large files lazy. Nexus has the same opportunity: workspace attach already runs a background walk through `BootIndexer` (`src/nexus/core/boot_indexer.py`) for search indexing. Layering eager hydration onto that attach phase converts N serial cold-misses into one parallel batch.

The `nexus-fuse` Rust daemon already exposes a Unix-socket JSON-RPC channel (`nexus-fuse/src/daemon.rs`) with handlers for `read`, `list`, `stat`, etc. `FileCache` (`nexus-fuse/src/cache.rs`) is the foyer hybrid cache with a public `put(path, content, etag, gen)` admission API.

## Decision

Add a new JSON-RPC method `cache_warm` to the existing nexus-fuse daemon. `BootIndexer` fires this method once per attach (after its existing search-walk completes) via the existing `RustFUSEClient`. Rust drives the entire hydration: list workspace, filter ≤128KiB, bounded-parallel fetch via `tokio::JoinSet`, admit to `FileCache`, return summary stats.

Workspace attach is not blocked: BootIndexer already runs in a daemon thread and `health_state["status"] = "ready"` is set independently of hydration. Hydration failure is a warning, not a fatal.

Warmth check for v1 is **existence + freshness** (cached entry exists AND `cached_at_secs` within `MAX_CACHE_AGE_SECS = 3600`). v1 passes `gen=0` to `FileCache::put` to keep generation handling out of scope. `FileMetadata` already carries `gen: u64` from the backend, so a v2 follow-up can pass that value directly once issue #4054 wires generation propagation end-to-end.

## Non-Goals

1. File-level generation tracking — handled by issue #4054.
2. Mid-session re-hydration — only fires once at attach.
3. Configurable per-workspace policy — single global threshold/budget for v1.
4. Hydrating large files (>128KiB) — explicitly out of scope.
5. Evicting existing cache entries to make room — budget caps admissions only; foyer's own LRU manages residency afterward.
6. New env-var configuration surface — defaults are constants; overrides come via RPC params.

## Component Layout

### Rust side (`nexus-fuse/src/`)

**`cache.rs`** — add three constants:

```rust
pub const HYDRATE_SMALL_FILE_BYTES: usize = 128 * 1024;       // 128 KiB
pub const HYDRATE_TOTAL_BUDGET_BYTES: usize = 64 * 1024 * 1024; // 64 MiB
pub const HYDRATE_CONCURRENCY: usize = 8;
```

Add a public method on `FileCache`:

```rust
pub fn is_warm(&self, path: &str) -> bool
```

Returns `true` iff metadata exists for `path` AND `cached_at_secs` is within `MAX_CACHE_AGE_SECS`. Reuses the existing `metadata: Mutex<HashMap<String, CacheMeta>>` — no foyer load needed.

**New `nexus-fuse/src/hydrate.rs`** — module containing:

```rust
pub struct HydrateOptions {
    pub workspace_root: String,
    pub threshold_bytes: usize,
    pub budget_bytes: usize,
    pub concurrency: usize,
}

#[derive(Serialize)]
pub struct HydrateStats {
    pub admitted_count: u64,
    pub admitted_bytes: u64,
    pub skipped_warm: u64,
    pub skipped_size: u64,
    pub skipped_budget: u64,
    pub failed: u64,
    pub duration_ms: u64,
}

pub async fn hydrate_workspace(
    client: Arc<NexusClient>,
    cache: Arc<FileCache>,
    opts: HydrateOptions,
) -> HydrateStats
```

Implementation outline:

1. Recursive walk via `client.list(path)` BFS from `opts.workspace_root`. `FileEntry.entry_type == "directory"` enqueues for further listing; everything else is treated as a file. Cap recursion depth or total entries defensively (default depth 32, total 100k entries) to bound memory if the backend has a misshapen tree. On any list error, log and continue with what was collected; if root list fails, return zero-stats with `failed = 1`.
2. While walking, drop entries with `size > threshold_bytes` (increment `skipped_size`) and entries where `cache.is_warm(path)` returns `true` (increment `skipped_warm`). Collect surviving full paths plus sizes into a `Vec<(String, u64)>`.
3. Spawn fetches into a `tokio::JoinSet` with a `tokio::sync::Semaphore::new(concurrency)` for back-pressure. Each task:
   - Acquire permit.
   - Check shared `admitted_bytes: Arc<AtomicU64>` against `budget_bytes`. If over, increment `skipped_budget`, return.
   - `client.read(path)` → bytes. On error, increment `failed`, return.
   - `cache.put(path, &bytes, etag.as_deref(), 0)`. `admitted_bytes.fetch_add(bytes.len() as u64)`.
4. Drain JoinSet. Materialize atomic counters into `HydrateStats`. Emit metrics (per-bucket file/byte counters, total duration).

Notes:
- Spawning continues even after the budget is reached; the per-task budget check is what halts admissions. Walk-order is preserved by spawning in BFS-listing order.
- A small race window exists where 2+ tasks see `admitted_bytes < budget` simultaneously and both admit; in the worst case `admitted_bytes` overshoots `budget_bytes` by up to `(concurrency - 1) * threshold_bytes`. This is acceptable.
- `client.read` already returns `etag` alongside bytes via the existing client struct; reuse it.

**`daemon.rs`** — register `"cache_warm"` in the dispatch match (around L246):

```rust
"cache_warm" => handle_cache_warm(&params, &client, file_cache.as_ref()).await,
```

Note: `cache_warm` is async (uses tokio), unlike the synchronous `spawn_blocking` handlers. Restructure dispatch so `cache_warm` runs directly on the tokio executor without `spawn_blocking`. Implementation:

```rust
async fn handle_cache_warm(
    params: &Value,
    client: &Arc<NexusClient>,
    cache: Option<&Arc<FileCache>>,
) -> Result<Value, NexusClientError>
```

Returns `serde_json::to_value(stats)` on success. Returns an `InvalidArgument` error if `cache` is `None` (operator must run with caching enabled).

**`metrics.rs`** — add four metric helpers:

```rust
pub fn record_hydration_file(result: &str)        // labels: admitted | skipped_warm | skipped_size | skipped_budget | failed
pub fn record_hydration_bytes(result: &str, n: u64) // labels: admitted | skipped
pub fn observe_hydration_duration_ms(ms: u64)
```

Counter names exposed to Prometheus:
- `nexus_hydration_files_total{result}`
- `nexus_hydration_bytes_total{result}`
- `nexus_hydration_duration_ms_total` (cumulative; histogram is overkill for once-per-attach)

### Python side

**`src/nexus/fuse/rust_client.py`** — add a new method on `RustFUSEClient`:

```python
def cache_warm(
    self,
    workspace_root: str,
    *,
    threshold_bytes: int | None = None,
    budget_bytes: int | None = None,
    concurrency: int | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"workspace_root": workspace_root}
    if threshold_bytes is not None:
        params["threshold_bytes"] = threshold_bytes
    if budget_bytes is not None:
        params["budget_bytes"] = budget_bytes
    if concurrency is not None:
        params["concurrency"] = concurrency
    return self._send_request("cache_warm", params)
```

Returns the parsed `HydrateStats` JSON.

**`src/nexus/core/boot_indexer.py`** — extend constructor and `_run`:

```python
class BootIndexer:
    def __init__(
        self,
        *,
        workspace: Path,
        search_daemon: Any,
        health_state: dict[str, Any],
        rust_client: RustFUSEClient | None = None,
        hydrate_threshold: int | None = None,
        hydrate_budget: int | None = None,
    ) -> None:
        ...

    def _run(self) -> None:
        try:
            self._walk_and_index()
        finally:
            self._health_state["status"] = "ready"

        if self._rust_client is not None:
            self._hydrate_cache()

    def _hydrate_cache(self) -> None:
        try:
            stats = self._rust_client.cache_warm(
                str(self._workspace),
                threshold_bytes=self._hydrate_threshold,
                budget_bytes=self._hydrate_budget,
            )
            log.info("cache hydration: %s", stats)
            self._health_state["hydration"] = stats
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            log.warning("cache hydration failed: %s", exc)
            self._health_state["hydration"] = {"error": str(exc)}
```

`_hydrate_cache` runs after `health_state["status"] = "ready"` so attach is never blocked by hydration latency. `_run` wraps the call in its own try-suite so a hydration error never affects the search-walk's "ready" transition.

**`src/nexus/daemon/sandbox_bootstrap.py`** — thread `rust_client` through. Around L157-164:

```python
indexer = BootIndexer(
    workspace=self._workspace,
    search_daemon=self._search_daemon,
    health_state=self._health_state,
    rust_client=self._rust_client,
)
```

`SandboxBootstrapper.__init__` already accepts `nexus_fs`; either expose `nexus_fs.rust_client` or add a separate constructor parameter `rust_client` plumbed from `main.py`. Choice deferred to implementation: whichever is cleaner once the actual `NexusFS` shape is read.

## Wire Format

JSON-RPC 2.0, all fields required unless marked optional.

**Request params** (`method = "cache_warm"`):

```json
{
  "workspace_root": "/",
  "threshold_bytes": 131072,    // optional, default 128 KiB
  "budget_bytes": 67108864,      // optional, default 64 MiB
  "concurrency": 8               // optional, default 8
}
```

**Response result**:

```json
{
  "admitted_count": 42,
  "admitted_bytes": 1234567,
  "skipped_warm": 5,
  "skipped_size": 100,
  "skipped_budget": 8,
  "failed": 0,
  "duration_ms": 320
}
```

**Errors**: standard JSON-RPC error envelope. Codes:
- `-32602` (invalid params) — workspace_root missing.
- `-32000` (server error) — file_cache disabled on daemon, list/IO failure during dispatch.

## Data Flow

1. `SandboxBootstrapper.run` constructs `BootIndexer` with `rust_client` reference (alongside existing `search_daemon`, `health_state`).
2. `BootIndexer.start_async()` spawns a daemon thread.
3. The thread runs `_walk_and_index` (search-indexing). On completion, sets `health_state["status"] = "ready"`.
4. The thread then calls `rust_client.cache_warm(workspace_root)`. Daemon thread blocks on this RPC; the main attach path has already returned.
5. Rust daemon dispatches to `handle_cache_warm` → `hydrate_workspace`:
   - BFS walk via `NexusClient.list` starting at `workspace_root`. Entries with `entry_type == "directory"` enqueue; otherwise treat as file.
   - Filter inline: drop entries > threshold; drop entries where `cache.is_warm(path)`. Collect survivors.
   - Spawn bounded-parallel `JoinSet` (concurrency=8). Each task: re-check budget atomically → `client.read(path)` → `cache.put(path, bytes, etag, gen=0)`. `admitted_bytes` updated atomically.
   - Drain `JoinSet`. Build `HydrateStats`. Emit metrics.
6. Daemon returns stats JSON. Python logs at INFO, stores in `health_state["hydration"]`.

## Error Handling

| Failure | Behavior |
|---|---|
| `NexusClient.list` returns error at root | Hydrate returns zero-stats with `failed=1`, logs error. Per-file fetches not attempted. |
| `NexusClient.list` returns error mid-walk on a sub-directory | Log warning, skip that subtree, continue with siblings. No `failed` increment (it isn't a per-file fetch failure). |
| Per-file `client.read` returns error | Increment `failed`, log at debug. Other files continue. |
| `FileCache.put` cannot admit (size > `max_file_size`) | Should not happen — pre-filtered to ≤128KiB. If it does, foyer silently rejects; we still increment `admitted_bytes` based on the bytes we passed in. (Future: detect and increment `failed` instead.) |
| RPC timeout / daemon dead | Python catches `BrokenPipeError`/`ConnectionResetError`/`OSError` from `_send_request`, logs warning, sets `health_state["hydration"] = {"error": str(exc)}`. Search-walk's "ready" status is unaffected. |
| `file_cache: None` on daemon | RPC returns `-32000` error. Python treats as warning. |

## Concurrency Model

- `FileCache.put` already serializes via `block_on_foyer` and the internal foyer runtime. Multiple concurrent admits from `JoinSet` tasks are safe.
- `admitted_bytes` is `Arc<AtomicU64>` shared across spawned tasks.
- Stats counters (`skipped_warm`, `skipped_size`, etc.) accumulate in the main `hydrate_workspace` task before spawning, except `admitted_count`, `failed`, `skipped_budget` which need atomic counters touched by the spawned tasks.
- Tokio semaphore caps in-flight fetches at `concurrency` (default 8).

## Testing

### Rust unit tests (`nexus-fuse/src/hydrate.rs`)

- `test_hydrate_admits_small_files` — `mockito` server returns 5 entries (3 small, 2 large). Assert `admitted_count == 3`, `skipped_size == 2`, FileCache contains the 3 small paths.
- `test_hydrate_skips_warm_entries` — pre-`put` two paths into FileCache. Run hydrate over a list including those + 1 cold. Assert `skipped_warm == 2`, `admitted_count == 1`, original cached bytes unchanged (no re-fetch verified via mockito request count).
- `test_hydrate_respects_budget` — 10 entries × 10KiB each, budget 30KiB. Assert `admitted_count` between 3 and `concurrency` (in-flight may overshoot by up to N-1 spawned tasks); assert `skipped_budget >= 1`.
- `test_hydrate_continues_on_per_file_error` — mockito returns 500 for one path. Assert `failed == 1`, other files admitted.
- `test_hydrate_metrics_emitted` — uses existing `metrics::test_guard()` pattern. Assert all four counters incremented with correct labels.
- `test_hydrate_empty_workspace` — list returns `[]`. Assert all-zero stats.
- `test_hydrate_list_failure` — mockito 500 on list. Assert `failed == 1`, `admitted_count == 0`.

### Rust IPC integration

Add `nexus-fuse/test_cache_warm.py` (sibling to existing `test_python_ipc.py`) — start daemon, create files via `sys_write`, call `cache_warm`, verify response shape and that subsequent `sys_read` is fast (cache hit observable via metrics).

### Python unit tests (`tests/unit/core/test_boot_indexer.py`)

- `test_boot_indexer_calls_cache_warm` — `MagicMock` rust_client. Assert `cache_warm` called once with workspace path. Health state still `ready`.
- `test_boot_indexer_handles_cache_warm_error` — rust_client raises `BrokenPipeError`. Assert `health_state["status"] == "ready"`, `health_state["hydration"]["error"]` set, no exception propagates.
- `test_boot_indexer_no_rust_client` — `rust_client=None`. Assert hydration is silently skipped, search-walk unaffected.
- `test_boot_indexer_passes_overrides` — pass `hydrate_threshold=4096`, assert RPC params include it.

### Benchmark (acceptance: cold-start small-read p50 ≥3× faster)

Extend `nexus-fuse/benches/cache_backends.rs` with two scenarios using `mockito` configured with a constant 10ms RTT:

- `cold_no_hydration_p50_50_files` — fresh cache, sequentially `cached_read::read_with_cache` 50 small files. Record p50 latency per file.
- `cold_with_hydration_p50_50_files` — fresh cache, run `hydrate_workspace`, then read same 50 files sequentially. Record p50.

Assert `cold_no_hydration / cold_with_hydration >= 3.0` in the bench's main block (panic on regression). Update `nexus-fuse/PERFORMANCE_RESULTS.md` with measured numbers from a real run.

## Telemetry

| Metric | Type | Labels | Increment site |
|---|---|---|---|
| `nexus_hydration_files_total` | Counter | `result=admitted\|skipped_warm\|skipped_size\|skipped_budget\|failed` | per filtered/processed entry |
| `nexus_hydration_bytes_total` | Counter | `result=admitted\|skipped` | when admitting (success bytes) or when skipping due to budget (would-be bytes) |
| `nexus_hydration_duration_ms_total` | Counter | none | once per `hydrate_workspace` call |

Existing `nexus_cache_*` counters from cache.rs continue to fire from inside `FileCache.put` admissions, giving an orthogonal view (`tier=dram` admit count rises during hydration).

## Open Questions

1. Should `_hydrate_cache` run on a *separate* daemon thread parallel to `_walk_and_index`, or strictly after? Spec assumes "after" for simplicity. Parallel would shave wall time on attach but doubles thread footprint. Defer until benchmark shows whether sequential walk-then-hydrate hurts.
2. Should we emit a `health_state["hydration"]` field for diagnostic readouts, or just rely on metrics? Spec says yes (cheap, helps debugging early in the rollout). Revisit if it bloats the health endpoint.

## References

- Issue #4055 (this design)
- Issue #4053 — foyer cache (parent infrastructure, landed)
- Issue #4054 — FileMetadata generation (parallel work; v2 warmth check will hook here)
- `nexus-fuse/src/cache.rs` — `FileCache` API
- `nexus-fuse/src/daemon.rs` — JSON-RPC dispatch
- `src/nexus/fuse/rust_client.py` — Python-side client
- `src/nexus/core/boot_indexer.py` — attach-time walker
