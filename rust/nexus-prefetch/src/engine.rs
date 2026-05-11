//! Top-level prefetch orchestrator.  Owns session map, worker pool,
//! bounded mpsc.  Public methods mirror the Python `ReadaheadManager`
//! surface — `on_open`, `on_read`, `on_release`, `shutdown` — so the
//! Python shim swaps with zero call-site changes.

use std::sync::Arc;
use bytes::Bytes;
use dashmap::DashMap;
use parking_lot::Mutex;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tracing::debug;
use std::sync::atomic::Ordering;

use crate::config::EngineConfig;
use crate::detector::{Detector, SequentialDetector};
use crate::metrics::{EngineMetrics, MetricsSnapshot};
use crate::pattern::AccessPattern;
use crate::range_reader::SharedRangeReader;
use crate::session::Session;
use crate::worker::{run_worker, PrefetchJob};

pub struct PrefetchEngine {
    cfg: EngineConfig,
    sessions: Arc<DashMap<u64, Arc<Mutex<Session>>>>,
    tx: mpsc::Sender<PrefetchJob>,
    metrics: Arc<EngineMetrics>,
    workers: Mutex<Vec<JoinHandle<()>>>,
    _runtime: Option<tokio::runtime::Runtime>,
    detector_factory: Box<dyn Fn() -> Box<dyn Detector> + Send + Sync>,
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
        let cfg = cfg.clamp();
        let metrics = Arc::new(EngineMetrics::default());
        let sessions = Arc::new(DashMap::new());
        let (tx, rx) = mpsc::channel(cfg.queue_capacity);
        let detector_factory: Box<dyn Fn() -> Box<dyn Detector> + Send + Sync> = {
            let tol = cfg.sequential_tolerance;
            let min = cfg.min_sequential_count;
            Box::new(move || Box::new(SequentialDetector::new(tol, min)))
        };

        let mut workers = Vec::with_capacity(cfg.max_workers);
        // Spawn exactly one consumer that fans out per-job onto
        // tokio's blocking pool inside run_worker. cfg.max_workers
        // controls the spawn_blocking pool size via the runtime
        // builder (configured by the caller).
        let h = match runtime.as_ref() {
            Some(rt) => rt.spawn(run_worker(
                rx,
                sessions.clone(),
                reader.clone(),
                metrics.clone(),
            )),
            None => tokio::spawn(run_worker(
                rx,
                sessions.clone(),
                reader.clone(),
                metrics.clone(),
            )),
        };
        workers.push(h);

        Self {
            cfg,
            sessions,
            tx,
            metrics,
            workers: Mutex::new(workers),
            _runtime: runtime,
            detector_factory,
        }
    }

    pub fn on_open(&self, fh: u64, path: &str, file_size: Option<u64>) {
        let det = (self.detector_factory)();
        let sess = Session::new(path.to_string(), fh, file_size, det, &self.cfg);
        self.sessions.insert(fh, Arc::new(Mutex::new(sess)));
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
            AccessPattern::Sequential | AccessPattern::Stride { .. } | AccessPattern::Trend { .. } => {
                s.grow_window();
                self.enqueue_prefetch(&mut s, offset, pattern);
            }
            AccessPattern::Random => {
                s.shrink_and_clear(self.cfg.initial_window);
                self.metrics.resets.fetch_add(1, Ordering::Relaxed);
            }
            AccessPattern::Cold => {}
        }

        let hit = s.take_range(offset, size, self.cfg.block_size);
        if hit.is_some() {
            self.metrics.hits.fetch_add(1, Ordering::Relaxed);
        } else {
            self.metrics.misses.fetch_add(1, Ordering::Relaxed);
            s.misses += 1;
        }
        hit
    }

    pub fn on_release(&self, fh: u64) {
        self.sessions.remove(&fh);
    }

    pub fn metrics(&self) -> MetricsSnapshot {
        self.metrics.snapshot()
    }

    pub fn shutdown(self) {
        drop(self.tx);
        let mut workers = self.workers.lock();
        for h in workers.drain(..) {
            h.abort();
        }
        // The owned runtime, if any, is dropped here — workers torn down.
    }

    fn enqueue_prefetch(&self, s: &mut Session, current_offset: u64, pattern: AccessPattern) {
        let block_size = self.cfg.block_size as u64;
        let first_block_offset = match pattern {
            AccessPattern::Sequential => ((current_offset / block_size) + 1) * block_size,
            AccessPattern::Stride { stride } if stride > 0 => {
                current_offset + stride as u64
            }
            AccessPattern::Stride { stride } if stride < 0 => {
                current_offset.saturating_sub((-stride) as u64)
            }
            AccessPattern::Trend { delta } if delta > 0 => current_offset + delta as u64,
            AccessPattern::Trend { delta } if delta < 0 => {
                current_offset.saturating_sub((-delta) as u64)
            }
            _ => return,
        };

        let max_offset = first_block_offset + s.window;
        let mut cur = first_block_offset;
        let mut issued = 0u32;
        while cur < max_offset && issued < self.cfg.max_blocks_per_trigger {
            if let Some(fs) = s.file_size {
                if cur >= fs {
                    break;
                }
            }
            if s.mark_pending(cur) {
                let job = PrefetchJob {
                    fh: s.fh,
                    key: s.path.clone(),
                    block_offset: cur,
                    block_size: self.cfg.block_size,
                };
                if let Err(_e) = self.tx.try_send(job) {
                    self.metrics
                        .dropped_backpressure
                        .fetch_add(1, Ordering::Relaxed);
                    s.pending.remove(&cur);
                    debug!(fh = s.fh, offset = cur, "queue full — dropping prefetch");
                    break;
                }
            }
            cur += block_size;
            issued += 1;
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
        };
        let reader_data = vec![0u8; 1 << 20];
        // Throttle the reader so jobs back up.
        struct SlowReader(Bytes);
        impl crate::range_reader::RangeReader for SlowReader {
            fn read(&self, _: &str, off: u64, sz: u32) -> Result<Bytes, crate::error::PrefetchError> {
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
        assert!(snap.dropped_backpressure > 0, "expected drop counter > 0, got {snap:?}");
    }
}
