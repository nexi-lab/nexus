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
    /// Generation token snapshotted from the session at enqueue time.
    /// The worker discards the read result if the session has since
    /// been invalidated or fh-reused (generation bumped).  Prevents
    /// stale bytes from landing in a live session after an
    /// invalidate_path/on_release/on_open sequence.
    pub generation: u64,
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
) {
    loop {
        let job = {
            let mut guard = rx.lock().await;
            match guard.recv().await {
                Some(j) => j,
                None => return, // sender dropped
            }
        };
        // Block-on the sync reader.  In future revisions we may switch
        // to an async-native trait, but the current `ObjectStore::read`
        // is sync (`rust/kernel/src/abc/object_store.rs:86`) so any
        // wrapper hops through `spawn_blocking` anyway.
        let fh = job.fh;
        let block_offset = job.block_offset;
        let generation = job.generation;
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
                    if s.generation != generation {
                        s.pending.remove(&block_offset);
                        debug!(
                            fh,
                            offset = block_offset,
                            job_gen = generation,
                            cur_gen = s.generation,
                            "prefetch generation mismatch; dropping bytes"
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
                    if s.generation == generation {
                        s.pending.remove(&block_offset);
                    }
                }
            }
            Err(join_err) => {
                warn!(error = %join_err, fh, offset = block_offset, "prefetch worker join failed");
                if let Some(slot) = sessions.get(&fh) {
                    let mut s = slot.lock();
                    if s.generation == generation {
                        s.pending.remove(&block_offset);
                    }
                }
            }
        }
    }
}
