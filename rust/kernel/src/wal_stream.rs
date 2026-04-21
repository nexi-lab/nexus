//! Durable DT_STREAM backed by Raft-replicated stream entries (R19.1b').
//!
//! Writes go through a dedicated ``Command::AppendStreamEntry`` — the
//! payload is raw bytes on the wire and in the state machine's
//! ``sm_stream_entries`` redb table. No ``FileMetadata`` round-trip,
//! no hex encoding, no overlap with file-metadata scans.
//!
//! R20.18.6: the pyclass wrapper is gone. Users reach WAL-backed
//! streams through the normal syscall surface — `sys_setattr(DT_STREAM,
//! io_profile="wal")` registers a `WalStreamCore` (which now impls
//! `StreamBackend`) with the kernel's `stream_manager`, after which
//! read/write land on the replicated raft log via the same Python
//! stream API as any other backend. No PyZoneHandle crosses the
//! boundary; kernel looks the zone up through `zone_manager_arc`.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

use nexus_raft::prelude::{Command, CommandResult, FullStateMachine, ZoneConsensus};

/// Minimal consensus surface the WAL needs — lets unit tests swap in
/// a mock without spinning up a full raft runtime.
pub trait WalConsensus: Send + Sync {
    fn append(&self, key: &str, data: &[u8]) -> Result<(), String>;
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String>;
}

/// Real raft-backed consensus — the production path.
pub struct RaftWalConsensus {
    node: ZoneConsensus<FullStateMachine>,
    runtime: tokio::runtime::Handle,
}

impl RaftWalConsensus {
    pub fn new(node: ZoneConsensus<FullStateMachine>, runtime: tokio::runtime::Handle) -> Self {
        Self { node, runtime }
    }
}

impl WalConsensus for RaftWalConsensus {
    fn append(&self, key: &str, data: &[u8]) -> Result<(), String> {
        let cmd = Command::AppendStreamEntry {
            key: key.to_string(),
            data: data.to_vec(),
        };
        let result = self
            .runtime
            .block_on(self.node.propose(cmd))
            .map_err(|e| format!("WAL propose({key}): {e}"))?;
        match result {
            CommandResult::Success => Ok(()),
            CommandResult::Error(e) => Err(format!("WAL apply({key}) rejected: {e}")),
            _ => Ok(()),
        }
    }

    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
        let key_owned = key.to_string();
        let fut = self
            .node
            .with_state_machine(move |sm: &FullStateMachine| sm.get_stream_entry(&key_owned));
        self.runtime
            .block_on(fut)
            .map_err(|e| format!("WAL get({key}): {e}"))
    }
}

/// Rust core — wraps a ``WalConsensus`` and tracks per-stream state.
pub struct WalStreamCore {
    consensus: Arc<dyn WalConsensus>,
    stream_id: String,
    prefix: String,
    next_seq: AtomicU64,
    closed: AtomicBool,
}

impl WalStreamCore {
    pub fn new(consensus: Arc<dyn WalConsensus>, stream_id: String) -> Self {
        let prefix = format!("/__wal_stream__/{stream_id}/");
        Self {
            consensus,
            stream_id,
            prefix,
            next_seq: AtomicU64::new(0),
            closed: AtomicBool::new(false),
        }
    }

    fn key(&self, seq: u64) -> String {
        format!("{}{seq}", self.prefix)
    }

    pub fn write_nowait(&self, data: &[u8]) -> Result<u64, String> {
        if self.closed.load(Ordering::Acquire) {
            return Err(format!("WAL stream {} is closed", self.stream_id));
        }
        // Atomic fetch_add: if two concurrent writers race, each gets
        // a unique seq. The raft apply is single-writer per key (no
        // overwrite race because seqs differ).
        let seq = self.next_seq.fetch_add(1, Ordering::AcqRel);
        let key = self.key(seq);
        self.consensus.append(&key, data)?;
        Ok(seq)
    }

    /// Read entry at ``seq``. ``Ok(Some(bytes))`` if present;
    /// ``Ok(None)`` if not yet written; ``Err`` if the stream is
    /// closed and no more data will arrive at this offset.
    pub fn read_at(&self, seq: u64) -> Result<Option<Vec<u8>>, String> {
        let key = self.key(seq);
        let bytes_opt = self.consensus.get(&key)?;
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

    /// Reachable via the `StreamBackend` trait impl below; retained as
    /// an inherent method for unit tests and future Rust-side probes.
    #[allow(dead_code)]
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
// StreamBackend impl for WalStreamCore — lets io_profile="wal" register a
// raft-backed stream with stream_manager alongside MemoryStreamBackend and
// SharedMemoryStreamBackend. Python never sees WalStreamCore directly;
// dispatch goes through the standard stream syscalls.
// ---------------------------------------------------------------------------

impl crate::stream::StreamBackend for WalStreamCore {
    fn push(&self, data: &[u8]) -> Result<usize, crate::stream::StreamError> {
        self.write_nowait(data)
            .map(|seq| seq as usize)
            .map_err(|_| crate::stream::StreamError::Closed("wal stream closed"))
    }

    fn read_at(&self, offset: usize) -> Result<(Vec<u8>, usize), crate::stream::StreamError> {
        match WalStreamCore::read_at(self, offset as u64) {
            Ok(Some(data)) => Ok((data, offset + 1)),
            Ok(None) => Err(crate::stream::StreamError::Empty),
            Err(_) => Err(crate::stream::StreamError::ClosedEmpty),
        }
    }

    fn read_batch(
        &self,
        offset: usize,
        count: usize,
    ) -> Result<(Vec<Vec<u8>>, usize), crate::stream::StreamError> {
        WalStreamCore::read_batch(self, offset as u64, count)
            .map(|(items, next)| (items, next as usize))
            .map_err(|_| crate::stream::StreamError::ClosedEmpty)
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
// Unit tests — in-memory WalConsensus mock, no raft runtime needed.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;
    use std::sync::Mutex;

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
            self.inner
                .lock()
                .unwrap()
                .insert(key.to_string(), data.to_vec());
            Ok(())
        }
        fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
            Ok(self.inner.lock().unwrap().get(key).cloned())
        }
    }

    fn core() -> WalStreamCore {
        WalStreamCore::new(Arc::new(MemConsensus::new()), "test".into())
    }

    #[test]
    fn write_then_read_single_entry() {
        let c = core();
        let seq = c.write_nowait(b"hello").unwrap();
        assert_eq!(seq, 0);
        let (data, _) = c.read_at(0).map(|o| (o.unwrap(), ())).unwrap();
        assert_eq!(data, b"hello");
        assert_eq!(c.tail(), 1);
    }

    #[test]
    fn write_many_preserves_order_and_seqs() {
        let c = core();
        for i in 0u8..10 {
            let seq = c.write_nowait(&[i, i + 1, i + 2]).unwrap();
            assert_eq!(seq, i as u64);
        }
        assert_eq!(c.tail(), 10);
        let (items, next) = c.read_batch(0, 100).unwrap();
        assert_eq!(items.len(), 10);
        assert_eq!(next, 10);
        for (i, item) in items.iter().enumerate() {
            assert_eq!(item, &[i as u8, i as u8 + 1, i as u8 + 2]);
        }
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
    fn stats_reflect_tail_and_closed() {
        let c = core();
        assert_eq!(c.tail(), 0);
        assert!(!c.is_closed());
        c.write_nowait(b"x").unwrap();
        c.write_nowait(b"y").unwrap();
        assert_eq!(c.tail(), 2);
        c.close();
        assert!(c.is_closed());
    }

    #[test]
    fn read_batch_stops_at_tail() {
        let c = core();
        c.write_nowait(b"1").unwrap();
        c.write_nowait(b"2").unwrap();
        let (items, next) = c.read_batch(0, 100).unwrap();
        assert_eq!(items.len(), 2);
        assert_eq!(next, 2);
    }

    #[test]
    fn read_batch_from_middle() {
        let c = core();
        for i in 0u8..5 {
            c.write_nowait(&[i]).unwrap();
        }
        let (items, next) = c.read_batch(2, 10).unwrap();
        assert_eq!(items.len(), 3);
        assert_eq!(next, 5);
        assert_eq!(items[0], vec![2]);
        assert_eq!(items[2], vec![4]);
    }

    #[test]
    fn binary_data_roundtrip_with_nullbytes() {
        let c = core();
        let payload = vec![0u8, 1, 0, 2, 0, 3, 0xff, 0x00, 0xfe];
        c.write_nowait(&payload).unwrap();
        assert_eq!(c.read_at(0).unwrap(), Some(payload));
    }

    /// Raw-byte roundtrip for arbitrary binary — no hex intermediate
    /// should remain (was a hot-path perf cost in the pre-R19.1b'
    /// design).
    #[test]
    fn binary_data_full_byte_range() {
        let c = core();
        let payload: Vec<u8> = (0u8..=255).collect();
        c.write_nowait(&payload).unwrap();
        assert_eq!(c.read_at(0).unwrap(), Some(payload));
    }
}
