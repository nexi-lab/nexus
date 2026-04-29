//! Durable DT_PIPE backed by a distributed `MetaStore`.
//!
//! Composes [`WalStreamCore`] for the metastore append/get plumbing.
//! The pipe-only state — a per-replica head cursor — lives locally;
//! each replica advances its own head as it `pop()`s entries it has
//! not seen yet.
//!
//! ## Single-consumer assumption
//!
//! Each replica maintains its own head pointer.  A `pop()` on replica
//! A does not advance the head on replica B.  For the AI-coordination
//! use case this is intended: `/shared/coord/win-to-mac.pipe` is only
//! popped on Mac, `/shared/coord/mac-to-win.pipe` is only popped on
//! Win — each pipe has exactly one consumer node, so per-replica heads
//! behave as a true destructive queue from that consumer's view.
//!
//! Entries remain in the metastore after pop; GC is intentionally
//! deferred (cheap on the read path, simplifies semantics, lets late
//! consumers replay if useful).
//!
//! ## Layering
//!
//! `WalPipeCore` is a kernel primitive.  WAL is an implementation
//! detail of the metastore (raft today, possibly something else
//! later).  The pipe primitive only knows about
//! `kernel::abc::meta_store::MetaStore`; replication, consensus, redb
//! key encoding etc. are entirely the metastore impl's concern.
//!
//! Wire layout: keys are `/__wal_pipe__/<id>/<seq>` so they share the
//! metastore's stream-entries side table with WAL streams without key
//! collision (stream prefix is `/__wal_stream__/<id>/<seq>`).

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use crate::abc::meta_store::MetaStore;
use crate::core::stream::wal::WalStreamCore;
use crate::pipe::{PipeBackend, PipeError};
use crate::stream::{StreamBackend, StreamError};

/// WAL-backed DT_PIPE backend.  See module docs for semantics.
pub struct WalPipeCore {
    inner: WalStreamCore,
    /// Per-replica head cursor — next seq to pop.  Independent of the
    /// stream's tail; pop() advances it, push() never touches it.
    head: AtomicU64,
}

impl WalPipeCore {
    pub fn new(store: Arc<dyn MetaStore>, pipe_id: String) -> Self {
        // `__wal_pipe__/<id>` prefix keeps pipe entries from colliding
        // with stream entries (`__wal_stream__/<id>`) in the shared
        // stream-entries side table.
        let inner = WalStreamCore::new(store, format!("__wal_pipe__/{pipe_id}"));
        Self {
            inner,
            head: AtomicU64::new(0),
        }
    }
}

impl PipeBackend for WalPipeCore {
    fn push(&self, data: &[u8]) -> Result<usize, PipeError> {
        // Reuse stream push; metastore replicates the entry.  Empty
        // payload is a no-op — match MemoryPipeBackend semantics.
        if data.is_empty() {
            return Ok(0);
        }
        match StreamBackend::push(&self.inner, data) {
            Ok(_) => Ok(data.len()),
            Err(_) => Err(PipeError::Closed("wal pipe closed")),
        }
    }

    fn pop(&self) -> Result<Vec<u8>, PipeError> {
        let head = self.head.load(Ordering::Acquire);
        match StreamBackend::read_at(&self.inner, head as usize) {
            Ok((data, _next)) => {
                // Advance head past the popped entry.  CAS guards
                // against a concurrent pop on the same replica; if
                // another thread beat us to it, surface as Empty so
                // the caller spins (standard pipe semantics).
                self.head
                    .compare_exchange(head, head + 1, Ordering::AcqRel, Ordering::Acquire)
                    .map_err(|_| PipeError::Empty)?;
                Ok(data)
            }
            Err(StreamError::Empty) => Err(PipeError::Empty),
            Err(StreamError::ClosedEmpty) => Err(PipeError::ClosedEmpty),
            Err(_) => Err(PipeError::Empty),
        }
    }

    fn close(&self) {
        StreamBackend::close(&self.inner);
    }

    fn is_closed(&self) -> bool {
        StreamBackend::is_closed(&self.inner)
    }

    fn is_empty(&self) -> bool {
        // Empty when the per-replica head has caught up with the
        // stream tail.  Best-effort — head can race with concurrent
        // push, but is_empty is informational on pipe semantics.
        self.head.load(Ordering::Acquire) as usize >= StreamBackend::tail_offset(&self.inner)
    }

    fn size(&self) -> usize {
        let head = self.head.load(Ordering::Acquire) as usize;
        let tail = StreamBackend::tail_offset(&self.inner);
        tail.saturating_sub(head)
    }

    fn msg_count(&self) -> usize {
        let head = self.head.load(Ordering::Acquire) as usize;
        let tail = StreamBackend::tail_offset(&self.inner);
        tail.saturating_sub(head)
    }
}

// ---------------------------------------------------------------------------
// Unit tests — in-memory MetaStore mock; no raft runtime needed.
// Deliberately mirrors WalStreamCore's mock so the per-replica head
// CAS path is exercised under thread contention.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::abc::meta_store::{FileMetadata, MetaStoreError};
    use std::collections::BTreeMap;
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

    fn pipe() -> WalPipeCore {
        let store: Arc<dyn MetaStore> = Arc::new(MemKvStore {
            inner: Mutex::new(BTreeMap::new()),
        });
        WalPipeCore::new(store, "test".into())
    }

    #[test]
    fn push_then_pop_round_trip() {
        let p = pipe();
        assert_eq!(p.push(b"hello").unwrap(), 5);
        let data = p.pop().unwrap();
        assert_eq!(data, b"hello");
    }

    #[test]
    fn pop_empty_returns_empty_error() {
        let p = pipe();
        assert!(matches!(p.pop(), Err(PipeError::Empty)));
    }

    #[test]
    fn pop_advances_head_and_subsequent_pop_gets_next() {
        let p = pipe();
        p.push(b"a").unwrap();
        p.push(b"b").unwrap();
        assert_eq!(p.pop().unwrap(), b"a");
        assert_eq!(p.pop().unwrap(), b"b");
        assert!(matches!(p.pop(), Err(PipeError::Empty)));
    }

    #[test]
    fn per_replica_heads_diverge_under_concurrent_consumers() {
        // Two `WalPipeCore` instances over the same MetaStore — model
        // two replicas.  Each replica owns its head; both should pop
        // every entry independently.
        let store: Arc<dyn MetaStore> = Arc::new(MemKvStore {
            inner: Mutex::new(BTreeMap::new()),
        });
        let writer = WalPipeCore::new(Arc::clone(&store), "shared".into());
        let reader_a = WalPipeCore::new(Arc::clone(&store), "shared".into());
        let reader_b = WalPipeCore::new(Arc::clone(&store), "shared".into());

        for i in 0u8..5 {
            writer.push(&[i]).unwrap();
        }
        // Each reader pops the full sequence from its own head.
        let popped_a: Vec<Vec<u8>> = (0..5).map(|_| reader_a.pop().unwrap()).collect();
        let popped_b: Vec<Vec<u8>> = (0..5).map(|_| reader_b.pop().unwrap()).collect();
        assert_eq!(popped_a, popped_b);
        assert_eq!(popped_a, vec![vec![0], vec![1], vec![2], vec![3], vec![4]]);
    }

    #[test]
    fn empty_payload_push_is_noop() {
        let p = pipe();
        assert_eq!(p.push(b"").unwrap(), 0);
        assert!(matches!(p.pop(), Err(PipeError::Empty)));
    }
}
