//! Top-level prefetch orchestrator.  Owns session map, worker pool,
//! bounded mpsc.  Public methods mirror the Python `ReadaheadManager`
//! surface — `on_open`, `on_read`, `on_release`, `shutdown` — so the
//! Python shim swaps with zero call-site changes.

use bytes::Bytes;
use dashmap::DashMap;
use parking_lot::Mutex;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tracing::debug;

use crate::config::EngineConfig;
use crate::detector::{Detector, MajorityTrendDetector, SequentialDetector, StrideDetector};
use crate::metrics::{EngineMetrics, MetricsSnapshot};
use crate::pattern::AccessPattern;
use crate::range_reader::SharedRangeReader;
use crate::session::Session;
use crate::worker::{run_worker, PrefetchJob, SharedReceiver};
use tokio::sync::Mutex as AsyncMutex;

/// Which detector each new session should be primed with.  Defaults
/// to `Sequential` (the safe choice for cold sessions); callers that
/// know their workload up-front can override via `PrefetchEngine::with_detector`.
#[derive(Debug, Clone, Copy)]
pub enum DetectorKind {
    Sequential,
    Stride,
    MajorityTrend,
}

pub struct PrefetchEngine {
    cfg: EngineConfig,
    sessions: Arc<DashMap<u64, Arc<Mutex<Session>>>>,
    /// path → set of fh — supports `invalidate_path` so the FUSE write
    /// hook can drop prefetched data after a write/delete.
    path_index: Arc<DashMap<String, std::collections::HashSet<u64>>>,
    tx: mpsc::Sender<PrefetchJob>,
    metrics: Arc<EngineMetrics>,
    workers: Mutex<Vec<JoinHandle<()>>>,
    runtime: Option<tokio::runtime::Runtime>,
    detector_factory: Box<dyn Fn() -> Box<dyn Detector> + Send + Sync>,
    /// Engine-wide monotonic counter that mints session identities.
    /// Issued on every `on_open` AND on every `invalidate_fh`/`clear`
    /// path so two different fh lifetimes (or same-fh post-invalidate)
    /// never share an id — workers compare this to reject stale
    /// deposits (round 3 finding #1).
    next_session_id: Arc<std::sync::atomic::AtomicU64>,
    /// Shared shutdown flag — set by `shutdown` so workers can drop
    /// queued jobs before issuing more backend reads (round 4 finding
    /// #4).  Lets a hung in-flight `read_callable` finish in the
    /// background without holding up unmount.
    shutdown_flag: Arc<std::sync::atomic::AtomicBool>,
    /// Global buffered-bytes counter — workers update via deposit's
    /// returned delta so cross-session OOM is bounded by
    /// `cfg.max_buffer_bytes` (round 7 finding #2).
    global_bytes: Arc<std::sync::atomic::AtomicU64>,
}

impl PrefetchEngine {
    /// Build with default Sequential detector and an owned tokio runtime.
    /// Caller can pass `None` for `runtime` if they want the engine to
    /// piggyback on an already-running runtime (it will then panic if
    /// `spawn` is called outside a runtime context).
    pub fn new(
        cfg: EngineConfig,
        reader: SharedRangeReader,
        runtime: Option<tokio::runtime::Runtime>,
    ) -> Self {
        Self::with_detector(cfg, reader, runtime, DetectorKind::Sequential)
    }

    /// Build with an explicit detector kind.  Workers are spawned
    /// `cfg.max_workers` deep, each consuming the shared receiver
    /// concurrently — that's the unit of in-flight backend GET
    /// parallelism.
    pub fn with_detector(
        cfg: EngineConfig,
        reader: SharedRangeReader,
        runtime: Option<tokio::runtime::Runtime>,
        detector: DetectorKind,
    ) -> Self {
        // Validate before clamping — clamp() only fixes max_window; the
        // rest of these invariants would otherwise panic later (round 3
        // finding #4).  We saturate-to-minimum rather than panic so
        // Python callers can be lenient with config dicts.
        let cfg = cfg.clamp().normalize();
        let metrics = Arc::new(EngineMetrics::default());
        let sessions = Arc::new(DashMap::new());
        let path_index = Arc::new(DashMap::new());
        let next_session_id = Arc::new(std::sync::atomic::AtomicU64::new(1));
        let shutdown_flag = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let global_bytes = Arc::new(std::sync::atomic::AtomicU64::new(0));
        let (tx, rx) = mpsc::channel(cfg.queue_capacity);
        let detector_factory: Box<dyn Fn() -> Box<dyn Detector> + Send + Sync> = {
            let tol = cfg.sequential_tolerance;
            let min = cfg.min_sequential_count;
            match detector {
                DetectorKind::Sequential => {
                    Box::new(move || Box::new(SequentialDetector::new(tol, min)))
                }
                DetectorKind::Stride => Box::new(move || Box::new(StrideDetector::new())),
                DetectorKind::MajorityTrend => {
                    Box::new(move || Box::new(MajorityTrendDetector::new()))
                }
            }
        };

        // Shared mpsc Receiver so all workers race for jobs.  The async
        // lock is held only during `recv`; blocking I/O runs unlocked.
        let shared_rx: SharedReceiver = Arc::new(AsyncMutex::new(rx));

        let worker_count = cfg.max_workers.max(1);
        let mut workers = Vec::with_capacity(worker_count);
        for _ in 0..worker_count {
            let h = match runtime.as_ref() {
                Some(rt) => rt.spawn(run_worker(
                    shared_rx.clone(),
                    sessions.clone(),
                    reader.clone(),
                    metrics.clone(),
                    shutdown_flag.clone(),
                    global_bytes.clone(),
                    cfg.max_buffer_bytes,
                )),
                None => tokio::spawn(run_worker(
                    shared_rx.clone(),
                    sessions.clone(),
                    reader.clone(),
                    metrics.clone(),
                    shutdown_flag.clone(),
                    global_bytes.clone(),
                    cfg.max_buffer_bytes,
                )),
            };
            workers.push(h);
        }

        Self {
            cfg,
            sessions,
            path_index,
            tx,
            metrics,
            workers: Mutex::new(workers),
            runtime,
            detector_factory,
            next_session_id,
            shutdown_flag,
            global_bytes,
        }
    }

    fn mint_session_id(&self) -> u64 {
        self.next_session_id
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed)
    }

    pub fn on_open(&self, fh: u64, path: &str, file_size: Option<u64>) {
        let det = (self.detector_factory)();
        // If the fh was reused (caller skipped on_release), drop any
        // existing session for that fh first.  Refund its buffered
        // bytes to the global counter so the cap stays honest across
        // fh churn (round 8 finding #2).
        if let Some((_, old_slot)) = self.sessions.remove(&fh) {
            let mut old = old_slot.lock();
            let drained = old.clear_returning_bytes();
            let old_path = old.path.clone();
            drop(old);
            self.refund_global(drained);
            if let Some(mut set) = self.path_index.get_mut(&old_path) {
                set.remove(&fh);
            }
        }
        let sess = Session::new(
            path.to_string(),
            fh,
            file_size,
            det,
            &self.cfg,
            self.mint_session_id(),
        );
        self.sessions.insert(fh, Arc::new(Mutex::new(sess)));
        self.path_index
            .entry(path.to_string())
            .or_default()
            .insert(fh);
    }

    /// Return prefetched bytes covering `[offset, offset+size)` if
    /// the engine already has them; otherwise `None` (caller falls
    /// back to backend read).  Always feeds the observation into the
    /// detector and may enqueue further prefetch jobs.
    pub fn on_read(&self, fh: u64, offset: u64, size: u32) -> Option<Bytes> {
        let slot = self.sessions.get(&fh)?;
        let mut s = slot.lock();

        // Drive detector first so window decisions reflect current obs.
        let pattern = s.detector.observe(offset, size);
        match pattern {
            AccessPattern::Sequential
            | AccessPattern::Stride { .. }
            | AccessPattern::Trend { .. } => {
                s.grow_window();
                self.enqueue_prefetch(&mut s, offset, size, pattern);
            }
            AccessPattern::Random => {
                s.shrink_and_clear(self.cfg.initial_window);
                self.metrics.resets.fetch_add(1, Ordering::Relaxed);
            }
            AccessPattern::Cold => {}
        }

        let (hit, evicted_hit) = s.take_range_with_delta(offset, size, self.cfg.block_size);
        let evicted_total = if hit.is_some() {
            self.metrics.hits.fetch_add(1, Ordering::Relaxed);
            evicted_hit
        } else {
            // Miss-path: forward note_read advances cursor + evicts
            // forward blocks; backward note_read just records position
            // (round 7 finding #4).
            let evicted_miss = s.note_read(offset, size);
            self.metrics.misses.fetch_add(1, Ordering::Relaxed);
            s.misses += 1;
            evicted_miss
        };
        if evicted_total > 0 {
            // Single atomic refund — `fetch_sub` is race-free against
            // concurrent worker deposits that use `fetch_add` upfront
            // (round 8 finding #2 — previous load+store could lose
            // concurrent worker reservations).
            self.refund_global(evicted_total);
        }
        hit
    }

    pub fn on_release(&self, fh: u64) {
        // Removing from sessions first means any in-flight prefetch
        // workers that haven't deposited yet see no Session entry and
        // drop their bytes silently (run_worker's existing fallback).
        let removed = self.sessions.remove(&fh);
        if let Some((_, slot)) = removed {
            // Drop buffer + pending eagerly so the next on_open of the
            // same fh after release doesn't accidentally see stale data,
            // AND refund the global counter (round 8 finding #2).
            let drained = slot.lock().clear_returning_bytes();
            self.refund_global(drained);
        }
        // Prune the path_index entry.
        let mut empty_paths: Vec<String> = Vec::new();
        for mut e in self.path_index.iter_mut() {
            e.value_mut().remove(&fh);
            if e.value().is_empty() {
                empty_paths.push(e.key().clone());
            }
        }
        for p in empty_paths {
            self.path_index.remove(&p);
        }
    }

    /// Drop all prefetched data + pending work for one file handle.
    /// Intended for FUSE write/delete invalidation when the caller
    /// still wants the session alive (read pattern restarts cold).
    /// Mints a fresh `session_id` so in-flight workers reject any
    /// pre-invalidate deposits, and refunds the global byte counter
    /// (round 8 finding #2).
    pub fn invalidate_fh(&self, fh: u64) {
        let drained = if let Some(slot) = self.sessions.get(&fh) {
            let mut s = slot.lock();
            let d = s.clear_returning_bytes();
            s.detector.reset();
            s.window = self.cfg.initial_window;
            s.session_id = self.mint_session_id();
            d
        } else {
            0
        };
        self.refund_global(drained);
    }

    fn refund_global(&self, bytes: u64) {
        if bytes > 0 {
            self.global_bytes
                .fetch_sub(bytes, std::sync::atomic::Ordering::Relaxed);
        }
    }

    /// Drop all prefetched data + pending work for every fh currently
    /// open on `path`.  Called from the Python `ReadaheadManager`
    /// invalidation hook (write/delete propagation).  Cheap when the
    /// path has no active sessions.
    pub fn invalidate_path(&self, path: &str) {
        let fhs: Vec<u64> = match self.path_index.get(path) {
            Some(set) => set.iter().copied().collect(),
            None => return,
        };
        for fh in fhs {
            self.invalidate_fh(fh);
        }
    }

    pub fn metrics(&self) -> MetricsSnapshot {
        self.metrics.snapshot()
    }

    /// Tear down the engine.  Sets the shared `shutdown_flag` so
    /// workers drop queued jobs without issuing more backend reads,
    /// closes the work queue, aborts the worker tasks, and (if the
    /// engine owns its runtime) hands the runtime off to
    /// `shutdown_timeout` so a hung backend `read` can't block
    /// unmount/remount indefinitely (round 3 finding #3, round 4
    /// finding #4 — timeout now configurable via
    /// `EngineConfig.shutdown_timeout_ms`).
    pub fn shutdown(mut self) {
        // Signal in-flight workers BEFORE closing the queue so they
        // see the flag on the next iteration and drain the queue
        // without issuing more reads.
        self.shutdown_flag
            .store(true, std::sync::atomic::Ordering::Release);
        // Close the queue so workers' `recv().await` returns None
        // on the next iteration and they exit cleanly.
        drop(self.tx);
        // Cancel any in-flight worker future.  `abort()` is best-effort
        // — a worker that is currently awaiting `spawn_blocking().await`
        // will only end after the OS-thread blocking task returns, which
        // is exactly what `shutdown_timeout` below bounds.
        let mut workers = self.workers.lock();
        for h in workers.drain(..) {
            h.abort();
        }
        drop(workers); // release before runtime teardown

        // If we own a runtime, give blocking tasks a bounded window to
        // finish (default 500ms is well above typical CAS-local reads
        // but short enough not to hang FUSE unmount on degraded
        // backends).  After the timeout the blocking pool is detached.
        if let Some(rt) = self.runtime.take() {
            rt.shutdown_timeout(std::time::Duration::from_millis(
                self.cfg.shutdown_timeout_ms,
            ));
        }
    }

    fn enqueue_prefetch(
        &self,
        s: &mut Session,
        current_offset: u64,
        current_size: u32,
        pattern: AccessPattern,
    ) {
        let block_size = self.cfg.block_size as u64;
        // Admission control: a prefetch block that exceeds the global
        // cap can never survive eviction, so issuing the backend read
        // is pure waste (round 9 finding #1).  Disable when the user
        // configures a cap smaller than block_size — typically a
        // misconfiguration where buffer_pool_mb=0 was passed through.
        if (self.cfg.block_size as u64) > self.cfg.max_buffer_bytes {
            return;
        }
        // For sequential we walk by block_size, starting at the block
        // that contains the byte AFTER the current read.  With small
        // FUSE reads (4 KiB) and a large block_size (4 MiB), this lands
        // on the block currently being consumed so subsequent reads
        // within the same block hit; for block-aligned reads it
        // naturally advances to the next block.  For stride/trend we
        // walk by the signed delta the detector observed, so positive
        // strides larger than a block actually fetch future stride
        // positions and negative strides walk backwards.
        let next_byte = current_offset.saturating_add(current_size as u64);
        // All prefetch offsets must be block-aligned: `Session::take_range`
        // looks up buffered bytes via `(offset / block_size) * block_size`,
        // so deposits must use the same key shape.  For Stride/Trend we
        // walk by the signed raw delta and snap every landing position
        // to its block boundary; the `mark_pending` dedup handles cases
        // where successive snaps collapse onto the same block.
        let snap = |off: u64| (off / block_size) * block_size;
        let (first_logical, step): (u64, i64) = match pattern {
            AccessPattern::Sequential => (next_byte, block_size as i64),
            AccessPattern::Stride { stride } | AccessPattern::Trend { delta: stride } => {
                if stride == 0 {
                    return;
                }
                let raw_first = if stride > 0 {
                    current_offset.saturating_add(stride.unsigned_abs())
                } else {
                    current_offset.saturating_sub(stride.unsigned_abs())
                };
                (raw_first, stride)
            }
            _ => return,
        };
        let mut logical = first_logical;
        let mut issued = 0u32;
        // Initialise `walked` with the distance from the current read
        // to the FIRST prefetch position.  Without this, a stride
        // larger than the window would still emit one prefetch far
        // outside the user's IO budget (round 7 finding #5).
        let mut walked: u64 = first_logical.abs_diff(current_offset);
        let step_abs = step.unsigned_abs();
        // Independent iteration budget — covers the case where stride
        // is much smaller than block_size and many logical positions
        // collapse onto a single block.  Without this, a 1-byte stride
        // with a 1 MiB window would spin millions of iterations holding
        // the session mutex (round 5 finding #1).  4× max_blocks_per_trigger
        // is enough slack for moderate snap collisions; pathological
        // configs still exit instead of stalling.
        let iter_budget = self.cfg.max_blocks_per_trigger.saturating_mul(4).max(8) as u64;
        let mut iters: u64 = 0;
        while walked < s.window && issued < self.cfg.max_blocks_per_trigger && iters < iter_budget {
            iters += 1;
            let key = snap(logical);
            if let Some(fs) = s.file_size {
                if key >= fs {
                    break;
                }
            }
            // `mark_pending` dedups against in-flight + buffered keys,
            // which is exactly the case where successive snaps collide
            // (e.g. stride=2K with block_size=4K).  Only count `issued`
            // when we actually emit a job — dup snaps shouldn't burn
            // the per-trigger budget.
            let did_issue = if s.mark_pending(key) {
                let job = PrefetchJob {
                    fh: s.fh,
                    key: s.path.clone(),
                    block_offset: key,
                    block_size: self.cfg.block_size,
                    session_id: s.session_id(),
                };
                if let Err(_e) = self.tx.try_send(job) {
                    self.metrics
                        .dropped_backpressure
                        .fetch_add(1, Ordering::Relaxed);
                    s.pending.remove(&key);
                    debug!(fh = s.fh, offset = key, "queue full — dropping prefetch");
                    break;
                }
                true
            } else {
                false
            };
            // Advance the logical cursor.  When `step_abs < block_size`
            // many successive iterations would snap to the same block;
            // we ALWAYS jump at least one block forward after either
            // issuing a job OR landing on a dup (mark_pending returned
            // false because the key is already in-flight/buffered).
            // That way sub-block strides hitting an already-pending
            // block don't burn the iter budget at the same key (round
            // 6 finding #4).  Issuing-by-stride is still preserved
            // when stride >= block_size.
            let advance_by = step_abs.max(block_size);
            let next = if step > 0 {
                logical.saturating_add(advance_by)
            } else {
                logical.saturating_sub(advance_by)
            };
            if next == logical {
                break; // saturated; stop issuing
            }
            logical = next;
            walked = walked.saturating_add(advance_by);
            if did_issue {
                issued += 1;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::range_reader::mock::MockRangeReader;
    use std::time::Duration;

    fn build_engine(data: Vec<u8>, cfg: EngineConfig) -> PrefetchEngine {
        let reader: SharedRangeReader = Arc::new(MockRangeReader::new(Bytes::from(data)));
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .unwrap();
        PrefetchEngine::new(cfg, reader, Some(rt))
    }

    #[test]
    fn miss_on_first_read_no_session() {
        let cfg = EngineConfig::default();
        let e = build_engine(vec![1u8; 1024], cfg);
        assert!(e.on_read(99, 0, 16).is_none());
    }

    #[test]
    fn sequential_workload_eventually_hits() {
        let cfg = EngineConfig {
            block_size: 16,
            initial_window: 64,
            max_window: 512,
            queue_capacity: 64,
            max_blocks_per_trigger: 4,
            sequential_tolerance: 0,
            min_sequential_count: 2,
            ..Default::default()
        };
        let e = build_engine(vec![7u8; 4096], cfg);
        e.on_open(1, "/x", Some(4096));

        // Warm-up reads (no prefetch yet).
        let _ = e.on_read(1, 0, 16);
        let _ = e.on_read(1, 16, 16);
        // After this read, sequential is confirmed and prefetch issues.
        let _ = e.on_read(1, 32, 16);

        // Give the worker time to deposit.
        std::thread::sleep(Duration::from_millis(200));

        // Next read should be a hit (block 48 was prefetched).
        let got = e.on_read(1, 48, 16);
        assert!(got.is_some(), "expected prefetched hit at offset 48");
        assert_eq!(&got.unwrap()[..], &[7u8; 16]);
    }

    #[test]
    fn release_removes_session() {
        let cfg = EngineConfig::default();
        let e = build_engine(vec![1u8; 1024], cfg);
        e.on_open(1, "/x", Some(1024));
        assert!(e.sessions.contains_key(&1));
        e.on_release(1);
        assert!(!e.sessions.contains_key(&1));
    }

    #[test]
    fn backpressure_drop_increments_metric() {
        // Tiny queue so the second job is rejected.
        let cfg = EngineConfig {
            block_size: 16,
            initial_window: 1024,
            max_window: 8192,
            queue_capacity: 1,
            max_blocks_per_trigger: 32,
            sequential_tolerance: 0,
            min_sequential_count: 2,
            max_workers: 1,
            shutdown_timeout_ms: 500,
            max_buffer_bytes: 1024 * 1024,
        };
        let reader_data = vec![0u8; 1 << 20];
        // Throttle the reader so jobs back up.
        struct SlowReader(Bytes);
        impl crate::range_reader::RangeReader for SlowReader {
            fn read(
                &self,
                _: &str,
                off: u64,
                sz: u32,
            ) -> Result<Bytes, crate::error::PrefetchError> {
                std::thread::sleep(Duration::from_millis(50));
                let end = (off + sz as u64) as usize;
                Ok(self.0.slice(off as usize..end.min(self.0.len())))
            }
        }
        let reader: SharedRangeReader = Arc::new(SlowReader(Bytes::from(reader_data)));
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()
            .unwrap();
        let e = PrefetchEngine::new(cfg, reader, Some(rt));
        e.on_open(1, "/x", Some(1 << 20));
        let _ = e.on_read(1, 0, 16);
        let _ = e.on_read(1, 16, 16);
        let _ = e.on_read(1, 32, 16); // triggers a big issue
                                      // Sleep less than the reader latency so queue stays full.
        std::thread::sleep(Duration::from_millis(10));
        let snap = e.metrics();
        assert!(
            snap.dropped_backpressure > 0,
            "expected drop counter > 0, got {snap:?}"
        );
    }
}
