//! Durable DT_STREAM backed by a distributed `MetaStore` (R19.1b').
//!
//! Writes append into the metastore's stream-entries side table —
//! `LocalMetaStore` returns `NotSupported` (so this backend only
//! activates when federation has installed a distributed impl like
//! `ZoneMetaStore`); `ZoneMetaStore` proposes
//! `Command::AppendStreamEntry` so peers see the entry via raft commit.
//! No `FileMetadata` round-trip, no hex encoding, no overlap with the
//! file-metadata key space.
//!
//! ## Layering
//!
//! `WalStreamCore` is a kernel primitive — it lives next to the other
//! `StreamBackend` impls in `crate::core::stream` and only knows about
//! the kernel-internal `MetaStore` HAL trait.  Replication (raft, future
//! alternatives) is the metastore impl's concern, not this struct's.
//! Federation-tier code never reaches in here directly.
//!
//! `write_nowait()` is genuinely non-blocking. Data lands in
//! an inflight `BTreeMap` (read-your-writes) and is drained to the
//! metastore by a dedicated background flush thread.  Hot-path cost:
//! one parking_lot `RwLock` write + channel `try_send` ≈ 50–200 ns.
//! The metastore propose happens entirely off the critical path.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc;
use std::sync::Arc;

use parking_lot::RwLock;

use crate::abc::meta_store::MetaStore;
use crate::stream::{StreamBackend, StreamError};

/// WAL-backed stream core.  Persists every write through the
/// distributed `MetaStore`'s stream-entries side table; serves
/// `read_at` from an inflight cache for read-your-writes before the
/// background flush confirms.
pub struct WalStreamCore {
    store: Arc<dyn MetaStore>,
    stream_id: String,
    prefix: String,
    next_seq: AtomicU64,
    closed: AtomicBool,
    flush_tx: mpsc::SyncSender<(u64, Vec<u8>)>,
    /// Written but not-yet-confirmed entries.  Checked first by
    /// `read_at` to guarantee read-your-writes without waiting for the
    /// metastore propose / flush.
    inflight: Arc<RwLock<BTreeMap<u64, Vec<u8>>>>,
}

impl WalStreamCore {
    /// Bounded flush channel depth.  4096 entries × ~4 KB average ≈ 16 MB
    /// peak memory before backpressure flips to synchronous write.
    const FLUSH_CHANNEL_CAP: usize = 4096;

    pub fn new(store: Arc<dyn MetaStore>, stream_id: String) -> Self {
        let prefix = format!("/__wal_stream__/{stream_id}/");
        let (flush_tx, flush_rx) = mpsc::sync_channel::<(u64, Vec<u8>)>(Self::FLUSH_CHANNEL_CAP);
        let inflight: Arc<RwLock<BTreeMap<u64, Vec<u8>>>> = Arc::new(RwLock::new(BTreeMap::new()));

        let inflight_bg = Arc::clone(&inflight);
        let store_bg = Arc::clone(&store);
        let prefix_bg = prefix.clone();
        let stream_id_bg = stream_id.clone();

        std::thread::Builder::new()
            .name(format!("wal-flush-{stream_id}"))
            .spawn(move || {
                while let Ok((seq, data)) = flush_rx.recv() {
                    let key = format!("{prefix_bg}{seq}");
                    match store_bg.append_stream_entry(&key, &data) {
                        Ok(()) => {
                            inflight_bg.write().remove(&seq);
                        }
                        Err(e) => {
                            // Entry stays in inflight — `read_at` remains
                            // available from the local cache; a peer
                            // catching up loses this entry, but
                            // surfacing the failure here would force
                            // every backend wake-up onto the syscall hot
                            // path.  Best-effort fan-out is the right
                            // tradeoff for an audit / coordination
                            // stream.
                            tracing::warn!(
                                stream_id = %stream_id_bg,
                                seq,
                                error = ?e,
                                "WAL flush failed; entry remains in inflight"
                            );
                        }
                    }
                }
            })
            .expect("failed to spawn WAL flush thread");

        Self {
            store,
            stream_id,
            prefix,
            next_seq: AtomicU64::new(0),
            closed: AtomicBool::new(false),
            flush_tx,
            inflight,
        }
    }

    fn key(&self, seq: u64) -> String {
        format!("{}{seq}", self.prefix)
    }

    pub fn write_nowait(&self, data: &[u8]) -> Result<u64, String> {
        if self.closed.load(Ordering::Acquire) {
            return Err(format!("WAL stream {} is closed", self.stream_id));
        }
        // Atomic fetch_add: concurrent writers each get a unique seq;
        // no overwrite race because seqs differ.
        let seq = self.next_seq.fetch_add(1, Ordering::AcqRel);
        let data_vec = data.to_vec();

        // Insert into inflight BEFORE enqueuing for flush so a
        // concurrent `read_at(seq)` finds the data immediately.
        self.inflight.write().insert(seq, data_vec.clone());

        match self.flush_tx.try_send((seq, data_vec.clone())) {
            Ok(()) => {}
            Err(mpsc::TrySendError::Full(_)) => {
                // Channel saturated (rare): flush synchronously to
                // avoid unbounded inflight growth.
                let key = self.key(seq);
                match self.store.append_stream_entry(&key, &data_vec) {
                    Ok(()) => {
                        self.inflight.write().remove(&seq);
                    }
                    Err(e) => {
                        tracing::warn!(
                            stream_id = %self.stream_id,
                            seq,
                            error = ?e,
                            "WAL sync-fallback flush failed"
                        );
                    }
                }
            }
            Err(mpsc::TrySendError::Disconnected(_)) => {
                // Background thread exited (WalStreamCore is being dropped).
            }
        }
        Ok(seq)
    }

    /// Append `data` and wait for the metastore commit before
    /// returning. Same allocation path as `write_nowait`, but bypasses
    /// the async flush channel so the call returns only after
    /// `store.append_stream_entry` has confirmed durability —
    /// i.e. raft has committed the entry on the local node and any
    /// peer reading the same store sees it immediately.
    ///
    /// Use this when the caller's contract is synchronous (e.g.
    /// `PipeBackend::push`, where pop on a sibling replica is
    /// expected to see the data). For high-throughput streams where
    /// the writer can pipeline multiple entries before any consumer
    /// reads, prefer `write_nowait`.
    pub fn write_sync(&self, data: &[u8]) -> Result<u64, String> {
        if self.closed.load(Ordering::Acquire) {
            return Err(format!("WAL stream {} is closed", self.stream_id));
        }
        let seq = self.next_seq.fetch_add(1, Ordering::AcqRel);
        let data_vec = data.to_vec();
        // Insert into inflight first so a `read_at(seq)` racing
        // between here and the store commit still finds the data.
        self.inflight.write().insert(seq, data_vec.clone());
        let key = self.key(seq);
        let result = self.store.append_stream_entry(&key, &data_vec);
        // Remove from inflight only on success; on failure the entry
        // stays in inflight so reads still return data even though
        // the metastore did not durably accept it (matches the
        // `write_nowait` flush-failure path).
        if result.is_ok() {
            self.inflight.write().remove(&seq);
        }
        result
            .map(|_| seq)
            .map_err(|e| format!("append_stream_entry({key}): {e:?}"))
    }

    /// Read the entry at `seq`.  `Ok(Some(bytes))` if present;
    /// `Ok(None)` if not yet written; `Err` if the stream is closed
    /// and no more data will arrive at this offset.
    pub fn read_at(&self, seq: u64) -> Result<Option<Vec<u8>>, String> {
        // Fast path: written but not yet flushed.
        if let Some(data) = self.inflight.read().get(&seq).cloned() {
            return Ok(Some(data));
        }
        // Slow path: background thread already confirmed this entry.
        let key = self.key(seq);
        let bytes_opt = self
            .store
            .get_stream_entry(&key)
            .map_err(|e| format!("get_stream_entry({key}): {e:?}"))?;
        match bytes_opt {
            Some(bytes) => Ok(Some(bytes)),
            None => {
                if self.closed.load(Ordering::Acquire) {
                    Err(format!("WAL stream {} closed at seq {seq}", self.stream_id))
                } else {
                    Ok(None)
                }
            }
        }
    }

    pub fn read_batch(&self, start_seq: u64, count: usize) -> Result<(Vec<Vec<u8>>, u64), String> {
        let mut items = Vec::with_capacity(count);
        let mut seq = start_seq;
        for _ in 0..count {
            match self.read_at(seq) {
                Ok(Some(data)) => {
                    items.push(data);
                    seq += 1;
                }
                Ok(None) => break,
                Err(_) if !items.is_empty() => break,
                Err(e) => return Err(e),
            }
        }
        Ok((items, seq))
    }

    pub fn close(&self) {
        self.closed.store(true, Ordering::Release);
    }

    pub fn is_closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    pub fn tail(&self) -> u64 {
        self.next_seq.load(Ordering::Acquire)
    }

    #[allow(dead_code)]
    pub fn stream_id(&self) -> &str {
        &self.stream_id
    }
}

// ---------------------------------------------------------------------------
// StreamBackend impl — `setattr_stream(io_profile="wal")` registers a
// WalStreamCore alongside MemoryStreamBackend and SharedMemoryStreamBackend.
// Python never sees WalStreamCore directly; dispatch goes through the
// standard stream syscalls.
// ---------------------------------------------------------------------------

impl StreamBackend for WalStreamCore {
    fn push(&self, data: &[u8]) -> Result<usize, StreamError> {
        self.write_nowait(data)
            .map(|seq| seq as usize)
            .map_err(|_| StreamError::Closed("wal stream closed"))
    }

    fn read_at(&self, offset: usize) -> Result<(Vec<u8>, usize), StreamError> {
        match WalStreamCore::read_at(self, offset as u64) {
            Ok(Some(data)) => Ok((data, offset + 1)),
            Ok(None) => Err(StreamError::Empty),
            Err(_) => Err(StreamError::ClosedEmpty),
        }
    }

    fn read_batch(
        &self,
        offset: usize,
        count: usize,
    ) -> Result<(Vec<Vec<u8>>, usize), StreamError> {
        WalStreamCore::read_batch(self, offset as u64, count)
            .map(|(items, next)| (items, next as usize))
            .map_err(|_| StreamError::ClosedEmpty)
    }

    fn close(&self) {
        WalStreamCore::close(self);
    }

    fn is_closed(&self) -> bool {
        WalStreamCore::is_closed(self)
    }

    fn tail_offset(&self) -> usize {
        WalStreamCore::tail(self) as usize
    }

    fn msg_count(&self) -> usize {
        WalStreamCore::tail(self) as usize
    }
}

// ---------------------------------------------------------------------------
// Unit tests — in-memory MetaStore mock, no raft runtime needed.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::abc::meta_store::{FileMetadata, MetaStoreError};
    use std::collections::HashSet;
    use std::sync::Mutex;

    struct MemKvStore {
        inner: Mutex<BTreeMap<String, Vec<u8>>>,
    }

    impl MetaStore for MemKvStore {
        fn get(&self, _path: &str) -> Result<Option<FileMetadata>, MetaStoreError> {
            Ok(None)
        }
        fn put(&self, _path: &str, _meta: FileMetadata) -> Result<(), MetaStoreError> {
            Ok(())
        }
        fn delete(&self, _path: &str) -> Result<bool, MetaStoreError> {
            Ok(false)
        }
        fn list(&self, _prefix: &str) -> Result<Vec<FileMetadata>, MetaStoreError> {
            Ok(Vec::new())
        }
        fn exists(&self, _path: &str) -> Result<bool, MetaStoreError> {
            Ok(false)
        }
        fn append_stream_entry(&self, key: &str, data: &[u8]) -> Result<(), MetaStoreError> {
            self.inner
                .lock()
                .unwrap()
                .insert(key.to_string(), data.to_vec());
            Ok(())
        }
        fn get_stream_entry(&self, key: &str) -> Result<Option<Vec<u8>>, MetaStoreError> {
            Ok(self.inner.lock().unwrap().get(key).cloned())
        }
    }

    fn core() -> WalStreamCore {
        let store: Arc<dyn MetaStore> = Arc::new(MemKvStore {
            inner: Mutex::new(BTreeMap::new()),
        });
        WalStreamCore::new(store, "test".into())
    }

    #[test]
    fn write_then_read_single_entry() {
        let c = core();
        let seq = c.write_nowait(b"hello").unwrap();
        assert_eq!(seq, 0);
        let data = c.read_at(0).unwrap().unwrap();
        assert_eq!(data, b"hello");
        assert_eq!(c.tail(), 1);
    }

    #[test]
    fn read_past_tail_returns_none_when_open() {
        let c = core();
        c.write_nowait(b"a").unwrap();
        assert_eq!(c.read_at(0).unwrap(), Some(b"a".to_vec()));
        assert_eq!(c.read_at(1).unwrap(), None);
    }

    #[test]
    fn read_past_tail_errors_when_closed() {
        let c = core();
        c.write_nowait(b"a").unwrap();
        c.close();
        assert!(c.read_at(1).is_err());
    }

    #[test]
    fn write_after_close_errors() {
        let c = core();
        c.close();
        assert!(c.write_nowait(b"x").is_err());
    }

    #[test]
    fn binary_data_full_byte_range() {
        let c = core();
        let payload: Vec<u8> = (0u8..=255).collect();
        c.write_nowait(&payload).unwrap();
        assert_eq!(c.read_at(0).unwrap(), Some(payload));
    }

    #[test]
    fn concurrent_writes_unique_seqs() {
        let c = Arc::new(core());
        let handles: Vec<_> = (0u8..8)
            .map(|i| {
                let c = Arc::clone(&c);
                std::thread::spawn(move || c.write_nowait(&[i]).unwrap())
            })
            .collect();
        let seqs: HashSet<u64> = handles.into_iter().map(|h| h.join().unwrap()).collect();
        assert_eq!(seqs.len(), 8);
        assert_eq!(c.tail(), 8);
        for seq in 0..8u64 {
            assert!(c.read_at(seq).unwrap().is_some());
        }
    }
}
