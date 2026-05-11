//! Worker task — consumes prefetch jobs off a bounded mpsc, calls the
//! injected `RangeReader`, deposits bytes into the requesting session.
//!
//! Spawned `max_workers` times by the engine.  All worker tasks share
//! a single `tokio::sync::Mutex<Receiver>` so they fan out true
//! concurrent I/O (each in-flight `spawn_blocking` only blocks the
//! worker that issued it, not the others).  Loops until the channel
//! is closed by `PrefetchEngine::shutdown`.

use crate::metrics::EngineMetrics;
use crate::range_reader::SharedRangeReader;
use crate::session::Session;
use dashmap::DashMap;
use parking_lot::Mutex;
use std::sync::Arc;
use tokio::sync::{mpsc, Mutex as AsyncMutex};
use tracing::{debug, warn};

pub struct PrefetchJob {
    pub fh: u64,
    pub key: String,
    pub block_offset: u64,
    pub block_size: u32,
    /// Globally unique session identity snapshotted from the session
    /// at enqueue time (engine mints from `next_session_id`).  Workers
    /// discard the read result if the current session for this fh has
    /// a different id — covering both invalidate-in-flight AND fh-reuse
    /// across distinct file lifetimes (round 3 finding #1).
    pub session_id: u64,
}

/// Multi-consumer receiver — workers race for jobs under a brief async
/// lock; the lock is released before the blocking read so workers run
/// in parallel.
pub type SharedReceiver = Arc<AsyncMutex<mpsc::Receiver<PrefetchJob>>>;

pub async fn run_worker(
    rx: SharedReceiver,
    sessions: Arc<DashMap<u64, Arc<Mutex<Session>>>>,
    reader: SharedRangeReader,
    metrics: Arc<EngineMetrics>,
    shutdown_flag: Arc<std::sync::atomic::AtomicBool>,
) {
    loop {
        let job = {
            let mut guard = rx.lock().await;
            match guard.recv().await {
                Some(j) => j,
                None => return, // sender dropped
            }
        };
        // Cancellation: if the engine is shutting down, drop queued
        // jobs without hitting the backend (round 4 finding #4).
        if shutdown_flag.load(std::sync::atomic::Ordering::Acquire) {
            if let Some(slot) = sessions.get(&job.fh) {
                let mut s = slot.lock();
                if s.session_id == job.session_id {
                    s.pending.remove(&job.block_offset);
                }
            }
            continue;
        }
        // Block-on the sync reader.  In future revisions we may switch
        // to an async-native trait, but the current `ObjectStore::read`
        // is sync (`rust/kernel/src/abc/object_store.rs:86`) so any
        // wrapper hops through `spawn_blocking` anyway.
        let fh = job.fh;
        let block_offset = job.block_offset;
        let session_id = job.session_id;
        let reader_clone = reader.clone();
        let result = tokio::task::spawn_blocking(move || {
            reader_clone.read(&job.key, job.block_offset, job.block_size)
        })
        .await;

        match result {
            Ok(Ok(bytes)) => {
                let size = bytes.len() as u64;
                if let Some(slot) = sessions.get(&fh) {
                    let mut s = slot.lock();
                    // Generation guard: session may have been invalidated
                    // or fh-reused while the backend read was in flight.
                    // Dropping the bytes is correct — the live session
                    // either re-issued the prefetch on its own terms or
                    // is intentionally fresh.
                    if s.session_id != session_id {
                        s.pending.remove(&block_offset);
                        debug!(
                            fh,
                            offset = block_offset,
                            job_sid = session_id,
                            cur_sid = s.session_id,
                            "prefetch session-id mismatch; dropping bytes"
                        );
                    } else {
                        s.deposit(block_offset, bytes);
                        metrics
                            .prefetched_bytes
                            .fetch_add(size, std::sync::atomic::Ordering::Relaxed);
                    }
                } else {
                    debug!(
                        fh,
                        offset = block_offset,
                        "prefetch landed after release; dropping"
                    );
                }
            }
            Ok(Err(e)) => {
                warn!(error = %e, fh, offset = block_offset, "prefetch read failed");
                if let Some(slot) = sessions.get(&fh) {
                    let mut s = slot.lock();
                    if s.session_id == session_id {
                        s.pending.remove(&block_offset);
                    }
                }
            }
            Err(join_err) => {
                warn!(error = %join_err, fh, offset = block_offset, "prefetch worker join failed");
                if let Some(slot) = sessions.get(&fh) {
                    let mut s = slot.lock();
                    if s.session_id == session_id {
                        s.pending.remove(&block_offset);
                    }
                }
            }
        }
    }
}
