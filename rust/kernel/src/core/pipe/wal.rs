//! Durable DT_PIPE backed by Raft-replicated entries (`io_profile="wal"`).
//!
//! Composes `WalStreamCore` to reuse the raft `AppendStreamEntry` /
//! `get_stream_entry` plumbing. The pipe-only state — a per-replica head
//! cursor — lives locally; each replica advances its own head as it
//! `pop()`s entries it has not seen yet.
//!
//! ## Single-consumer assumption
//!
//! Each replica maintains its own head pointer. A `pop()` on replica A
//! does not advance the head on replica B. For the AI-coordination use
//! case this is intended: `/shared/coord/win-to-mac.pipe` is only popped
//! on Mac, `/shared/coord/mac-to-win.pipe` is only popped on Win — each
//! pipe has exactly one consumer node, so per-replica heads behave as a
//! true destructive queue from that consumer's view.
//!
//! Entries remain in the raft state machine after pop; GC is intentionally
//! deferred (cheap on the read path, simplifies semantics, lets late
//! consumers replay if useful). Wire `Command::DeleteStreamEntry` later
//! if memory pressure becomes a concern.
//!
//! Wire layout: keys are `/__wal_pipe__/<id>/<seq>` so they share the
//! state machine's `TREE_STREAM_ENTRIES` table with WAL streams without
//! key collision (stream prefix is `/__wal_stream__/<id>/<seq>`).

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use crate::pipe::{PipeBackend, PipeError};
use crate::stream::StreamBackend;
use crate::wal_stream::{WalConsensus, WalStreamCore};

/// WAL-replicated DT_PIPE backend. See module docs for semantics.
pub(crate) struct WalPipeCore {
    inner: WalStreamCore,
    /// Per-replica head cursor — next seq to pop. Independent of the
    /// stream's tail; pop() advances it, push() never touches it.
    head: AtomicU64,
}

impl WalPipeCore {
    pub(crate) fn new(consensus: Arc<dyn WalConsensus>, pipe_id: String) -> Self {
        // `__wal_pipe__/<id>` instead of `__wal_stream__/<id>` so the two
        // share `TREE_STREAM_ENTRIES` without key collision.
        let inner = WalStreamCore::new(consensus, format!("__wal_pipe__/{pipe_id}"));
        Self {
            inner,
            head: AtomicU64::new(0),
        }
    }
}

impl PipeBackend for WalPipeCore {
    fn push(&self, data: &[u8]) -> Result<usize, PipeError> {
        // Reuse stream push; raft replicates the entry. Empty payload is a
        // no-op for streams; mirror that here to match MemoryPipeBackend.
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
                // Advance head past the popped entry. CAS guards against
                // a concurrent pop on the same replica; if another thread
                // beat us to it, retry from its new head.
                self.head
                    .compare_exchange(head, head + 1, Ordering::AcqRel, Ordering::Acquire)
                    .map_err(|_| PipeError::Empty)?;
                Ok(data)
            }
            Err(crate::stream::StreamError::Empty) => Err(PipeError::Empty),
            Err(crate::stream::StreamError::ClosedEmpty) => Err(PipeError::ClosedEmpty),
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
        // Empty from this replica's view: head has caught up to the
        // stream tail. Other replicas may have a different head.
        let tail = StreamBackend::tail_offset(&self.inner) as u64;
        self.head.load(Ordering::Acquire) >= tail
    }

    fn size(&self) -> usize {
        // Pending payload bytes — unknown without summing entry sizes,
        // and the WAL stream API doesn't surface that cheaply. Report 0
        // to match the `RemotePipeBackend` convention; consumers that
        // care use `msg_count` instead.
        0
    }

    fn msg_count(&self) -> usize {
        let tail = StreamBackend::tail_offset(&self.inner) as u64;
        let head = self.head.load(Ordering::Acquire);
        tail.saturating_sub(head) as usize
    }
}

// ---------------------------------------------------------------------------
// Tests — in-memory WalConsensus mock, no raft runtime needed.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use parking_lot::Mutex;
    use std::collections::BTreeMap;

    struct MemConsensus {
        inner: Mutex<BTreeMap<String, Vec<u8>>>,
    }

    impl MemConsensus {
        fn new() -> Self {
            Self {
                inner: Mutex::new(BTreeMap::new()),
            }
        }
    }

    impl WalConsensus for MemConsensus {
        fn append(&self, key: &str, data: &[u8]) -> Result<(), String> {
            self.inner.lock().insert(key.to_string(), data.to_vec());
            Ok(())
        }
        fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
            Ok(self.inner.lock().get(key).cloned())
        }
    }

    fn pipe(id: &str) -> WalPipeCore {
        WalPipeCore::new(Arc::new(MemConsensus::new()), id.to_string())
    }

    #[test]
    fn push_then_pop_roundtrip() {
        let p = pipe("t1");
        assert_eq!(p.push(b"hello").unwrap(), 5);
        assert_eq!(p.pop().unwrap(), b"hello");
    }

    #[test]
    fn pop_empty_pipe_returns_empty_error() {
        let p = pipe("t2");
        assert!(matches!(p.pop(), Err(PipeError::Empty)));
    }

    #[test]
    fn fifo_ordering_preserved() {
        let p = pipe("t3");
        for i in 0u8..5 {
            p.push(&[i]).unwrap();
        }
        for i in 0u8..5 {
            assert_eq!(p.pop().unwrap(), vec![i]);
        }
        assert!(matches!(p.pop(), Err(PipeError::Empty)));
    }

    #[test]
    fn msg_count_reflects_unconsumed_only() {
        let p = pipe("t4");
        p.push(b"a").unwrap();
        p.push(b"b").unwrap();
        p.push(b"c").unwrap();
        assert_eq!(p.msg_count(), 3);
        p.pop().unwrap();
        assert_eq!(p.msg_count(), 2);
        p.pop().unwrap();
        p.pop().unwrap();
        assert_eq!(p.msg_count(), 0);
    }

    #[test]
    fn is_empty_tracks_local_head_vs_tail() {
        let p = pipe("t5");
        assert!(p.is_empty());
        p.push(b"x").unwrap();
        assert!(!p.is_empty());
        p.pop().unwrap();
        assert!(p.is_empty());
    }

    #[test]
    fn close_then_pop_drains_then_signals_closed_empty() {
        let p = pipe("t6");
        p.push(b"final").unwrap();
        p.close();
        assert!(p.is_closed());
        assert_eq!(p.pop().unwrap(), b"final");
        assert!(matches!(p.pop(), Err(PipeError::ClosedEmpty)));
    }

    #[test]
    fn empty_push_is_noop() {
        let p = pipe("t7");
        assert_eq!(p.push(b"").unwrap(), 0);
        assert_eq!(p.msg_count(), 0);
    }

    #[test]
    fn binary_payload_with_null_bytes_roundtrip() {
        let p = pipe("t8");
        let payload = vec![0u8, 1, 0, 2, 0xff, 0x00, 0xfe];
        p.push(&payload).unwrap();
        assert_eq!(p.pop().unwrap(), payload);
    }

    /// Two `WalPipeCore` instances over the same consensus mock represent
    /// two replicas. Each maintains its own head — a pop on replica A
    /// does NOT advance B's head, so B can replay the same message. This
    /// is the documented "single-consumer" semantic.
    ///
    /// Replica B can only see A's push after the bg flush thread has
    /// committed it to the shared consensus (B has its own inflight map
    /// so A's pre-flush state is invisible). Poll briefly to dodge the
    /// async-flush race rather than sleeping a fixed duration.
    #[test]
    fn per_replica_heads_diverge_under_concurrent_consumers() {
        use std::time::{Duration, Instant};
        let consensus = Arc::new(MemConsensus::new());
        let a = WalPipeCore::new(
            Arc::clone(&consensus) as Arc<dyn WalConsensus>,
            "shared".into(),
        );
        let b = WalPipeCore::new(
            Arc::clone(&consensus) as Arc<dyn WalConsensus>,
            "shared".into(),
        );
        a.push(b"msg-1").unwrap();
        assert_eq!(a.pop().unwrap(), b"msg-1");

        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            match b.pop() {
                Ok(data) => {
                    assert_eq!(data, b"msg-1");
                    return;
                }
                Err(PipeError::Empty) if Instant::now() < deadline => {
                    std::thread::sleep(Duration::from_millis(5));
                }
                other => panic!("expected Ok(msg-1) within 2s, got {other:?}"),
            }
        }
    }
}
