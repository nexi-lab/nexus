//! Worker task — consumes prefetch jobs off a bounded mpsc, calls the
//! injected `RangeReader`, deposits bytes into the requesting session.
//!
//! Spawned `max_workers` times by the engine.  Loops until the
//! channel is closed by `PrefetchEngine::shutdown`.

use std::sync::Arc;
use tokio::sync::mpsc;
use dashmap::DashMap;
use parking_lot::Mutex;
use tracing::{debug, warn};
use crate::range_reader::SharedRangeReader;
use crate::session::Session;
use crate::metrics::EngineMetrics;

pub struct PrefetchJob {
    pub fh: u64,
    pub key: String,
    pub block_offset: u64,
    pub block_size: u32,
}

pub async fn run_worker(
    mut rx: mpsc::Receiver<PrefetchJob>,
    sessions: Arc<DashMap<u64, Arc<Mutex<Session>>>>,
    reader: SharedRangeReader,
    metrics: Arc<EngineMetrics>,
) {
    while let Some(job) = rx.recv().await {
        // Block-on the sync reader.  In future revisions we may switch
        // to an async-native trait, but the current `ObjectStore::read`
        // is sync (`rust/kernel/src/abc/object_store.rs:86`) so any
        // wrapper hops through `spawn_blocking` anyway.
        let fh = job.fh;
        let block_offset = job.block_offset;
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
                    s.deposit(block_offset, bytes);
                    metrics
                        .prefetched_bytes
                        .fetch_add(size, std::sync::atomic::Ordering::Relaxed);
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
                    slot.lock().pending.remove(&block_offset);
                }
            }
            Err(join_err) => {
                warn!(error = %join_err, "prefetch worker join failed");
            }
        }
    }
}
