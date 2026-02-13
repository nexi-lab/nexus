//! Replication WAL for True Local-First EC writes.
//!
//! EC writes bypass Raft consensus and apply directly to the local state machine.
//! This log records those writes for async replication to peers (Phase C, future).
//!
//! The WAL sequence number serves as the WriteToken — callers poll
//! `is_committed(seq)` to check if a write has been replicated to a majority.
//!
//! # Token Semantics
//!
//! - `is_committed(token)` returns:
//!   - `Some("committed")` if `token <= replicated_watermark`
//!   - `Some("pending")` if `token > replicated_watermark && token < next_seq`
//!   - `None` if `token >= next_seq` (invalid / unknown)
//!
//! # No Eviction
//!
//! Tokens never expire. The watermark is a single u64 comparison — O(1).
//! This elegantly handles the "disconnected overnight" scenario: tokens stay
//! "pending" while partitioned, flip to "committed" on reconnect.

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

use crate::storage::RedbTree;

use super::Result;

/// Redb tree name for the EC replication log entries.
const TREE_REPLICATION_LOG: &str = "ec_replication_log";
/// Redb tree name for replication metadata (watermarks, counters).
const TREE_REPLICATION_META: &str = "ec_replication_meta";
/// Key for persisted next sequence number.
const KEY_NEXT_SEQ: &[u8] = b"__next_seq__";
/// Key for persisted replicated watermark.
const KEY_REPLICATED_WATERMARK: &[u8] = b"__replicated_watermark__";
/// Key for persisted earliest sequence number (compaction lower bound).
const KEY_EARLIEST_SEQ: &[u8] = b"__earliest_seq__";

/// An entry in the EC replication WAL.
///
/// Stored in redb keyed by sequence number (u64 big-endian).
/// Used by the background replication task (Phase C) to send writes to peers.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReplicationEntry {
    /// Serialized `Command` bytes.
    pub command: Vec<u8>,
    /// Wall clock timestamp (Unix seconds) for LWW conflict resolution.
    pub timestamp: u64,
    /// Node ID of the writer (deterministic tie-breaking for LWW).
    pub node_id: u64,
}

/// Write-ahead log for EC (eventually consistent) writes.
///
/// Thread-safe: all methods take `&self`. Sequence counter and watermark use
/// atomics; redb handles write transaction serialization internally.
///
/// Shared between the [`ZoneConsensus`] handle (EC writes) and the driver
/// (watermark updates) via `Arc<ReplicationLog>`.
pub struct ReplicationLog {
    /// Replication log entries: seq (u64 BE) → ReplicationEntry.
    log_tree: RedbTree,
    /// Metadata: next_seq, replicated_watermark.
    meta_tree: RedbTree,
    /// Next sequence number to assign (monotonically increasing, starts at 1).
    next_seq: AtomicU64,
    /// Highest sequence number replicated to a majority of peers.
    replicated_watermark: AtomicU64,
    /// Earliest sequence number still in the WAL (compaction lower bound).
    earliest_seq: AtomicU64,
    /// This node's ID (for LWW tie-breaking in ReplicationEntry).
    node_id: u64,
}

impl ReplicationLog {
    /// Create or restore a ReplicationLog from the given redb store.
    ///
    /// Persisted state (next_seq, watermark) is restored from the meta tree.
    /// If fresh, next_seq starts at 1 (0 is reserved for "no token").
    pub fn new(store: &crate::storage::RedbStore, node_id: u64) -> Result<Self> {
        let log_tree = store.tree(TREE_REPLICATION_LOG)?;
        let meta_tree = store.tree(TREE_REPLICATION_META)?;

        // Restore persisted next_seq
        let next_seq = meta_tree
            .get(KEY_NEXT_SEQ)?
            .and_then(|v| v.try_into().ok().map(u64::from_be_bytes))
            .unwrap_or(1); // Start at 1 (0 = no token)

        // Restore persisted replicated watermark
        let replicated_watermark = meta_tree
            .get(KEY_REPLICATED_WATERMARK)?
            .and_then(|v| v.try_into().ok().map(u64::from_be_bytes))
            .unwrap_or(0);

        // Restore persisted earliest_seq (compaction lower bound)
        let earliest_seq = meta_tree
            .get(KEY_EARLIEST_SEQ)?
            .and_then(|v| v.try_into().ok().map(u64::from_be_bytes))
            .unwrap_or(1); // Start at 1 (same as next_seq)

        tracing::debug!(
            next_seq,
            replicated_watermark,
            earliest_seq,
            node_id,
            "ReplicationLog initialized"
        );

        Ok(Self {
            log_tree,
            meta_tree,
            next_seq: AtomicU64::new(next_seq),
            replicated_watermark: AtomicU64::new(replicated_watermark),
            earliest_seq: AtomicU64::new(earliest_seq),
            node_id,
        })
    }

    /// Append a command to the replication log.
    ///
    /// Returns the sequence number which serves as the WriteToken.
    /// The caller should have already applied this command to the local
    /// state machine before calling this.
    pub fn append(&self, command_bytes: &[u8]) -> Result<u64> {
        let seq = self.next_seq.fetch_add(1, Ordering::SeqCst);

        let entry = ReplicationEntry {
            command: command_bytes.to_vec(),
            timestamp: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs(),
            node_id: self.node_id,
        };

        let key = seq.to_be_bytes();
        let value = bincode::serialize(&entry)?;
        self.log_tree.set(&key, &value)?;

        // Persist next_seq so we don't reuse sequence numbers after restart
        self.meta_tree.set(KEY_NEXT_SEQ, &(seq + 1).to_be_bytes())?;

        tracing::trace!(seq, "EC write appended to replication log");
        Ok(seq)
    }

    /// Check if a write token has been committed (replicated to majority).
    ///
    /// Returns:
    /// - `Some("committed")` — write has been replicated
    /// - `Some("pending")` — write is local-only, awaiting replication
    /// - `None` — invalid token (0, or >= next_seq)
    pub fn is_committed(&self, token: u64) -> Option<&str> {
        let max = self.next_seq.load(Ordering::SeqCst);
        if token == 0 || token >= max {
            return None; // invalid or unknown token
        }

        let watermark = self.replicated_watermark.load(Ordering::SeqCst);
        if token <= watermark {
            Some("committed")
        } else {
            Some("pending")
        }
    }

    /// Get the next sequence number (exclusive upper bound).
    pub fn max_seq(&self) -> u64 {
        self.next_seq.load(Ordering::SeqCst)
    }

    /// Advance the replicated watermark after peer confirmation.
    ///
    /// Called by the background replication task (Phase C) when writes
    /// have been acknowledged by a majority of peers.
    pub fn advance_watermark(&self, new_watermark: u64) -> Result<()> {
        let current = self.replicated_watermark.load(Ordering::SeqCst);
        if new_watermark > current {
            self.replicated_watermark
                .store(new_watermark, Ordering::SeqCst);
            self.meta_tree
                .set(KEY_REPLICATED_WATERMARK, &new_watermark.to_be_bytes())?;
            tracing::debug!(
                old = current,
                new = new_watermark,
                "Replicated watermark advanced"
            );
        }
        Ok(())
    }

    /// Get all unreplicated entries (seq > watermark).
    ///
    /// Used by the background replication task (Phase C) to send pending
    /// writes to peers.
    pub fn drain_unreplicated(&self) -> Result<Vec<(u64, ReplicationEntry)>> {
        let watermark = self.replicated_watermark.load(Ordering::SeqCst);
        let max = self.next_seq.load(Ordering::SeqCst);

        let mut entries = Vec::new();
        for seq in (watermark + 1)..max {
            let key = seq.to_be_bytes();
            if let Some(data) = self.log_tree.get(&key)? {
                let entry: ReplicationEntry = bincode::deserialize(&data)?;
                entries.push((seq, entry));
            }
        }

        Ok(entries)
    }

    /// Get the earliest sequence number still in the WAL.
    ///
    /// Used for anti-entropy detection: if a peer's `acked_seq` is less than
    /// this value, it has fallen behind the compacted region and needs a
    /// full snapshot instead of incremental replication.
    pub fn earliest_seq(&self) -> u64 {
        self.earliest_seq.load(Ordering::SeqCst)
    }

    /// Remove WAL entries with seq <= `up_to_seq` (Kafka-style compaction).
    ///
    /// Called after all peers have consumed these entries. Returns the number
    /// of entries deleted from the log tree.
    ///
    /// Safe to call concurrently — `earliest_seq` is updated atomically and
    /// persisted to the meta tree for crash recovery.
    pub fn compact(&self, up_to_seq: u64) -> Result<u64> {
        let earliest = self.earliest_seq.load(Ordering::SeqCst);
        if up_to_seq < earliest {
            return Ok(0); // Already compacted past this point
        }

        let mut deleted = 0u64;
        for seq in earliest..=up_to_seq {
            if self.log_tree.delete(&seq.to_be_bytes())?.is_some() {
                deleted += 1;
            }
        }

        let new_earliest = up_to_seq + 1;
        self.earliest_seq.store(new_earliest, Ordering::SeqCst);
        self.meta_tree
            .set(KEY_EARLIEST_SEQ, &new_earliest.to_be_bytes())?;

        tracing::debug!(from = earliest, to = up_to_seq, deleted, "WAL compacted");
        Ok(deleted)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::RedbStore;

    #[test]
    fn test_replication_log_basic() {
        let store = RedbStore::open_temporary().unwrap();
        let log = ReplicationLog::new(&store, 1).unwrap();

        // Fresh log: no valid tokens
        assert_eq!(log.max_seq(), 1);
        assert!(log.is_committed(0).is_none()); // 0 is reserved
        assert!(log.is_committed(1).is_none()); // not yet written

        // Append a command
        let seq1 = log.append(b"cmd1").unwrap();
        assert_eq!(seq1, 1);
        assert_eq!(log.is_committed(1), Some("pending"));
        assert!(log.is_committed(2).is_none()); // not yet written

        // Append another
        let seq2 = log.append(b"cmd2").unwrap();
        assert_eq!(seq2, 2);
        assert_eq!(log.is_committed(1), Some("pending"));
        assert_eq!(log.is_committed(2), Some("pending"));

        // Advance watermark
        log.advance_watermark(1).unwrap();
        assert_eq!(log.is_committed(1), Some("committed"));
        assert_eq!(log.is_committed(2), Some("pending"));

        // Advance to 2
        log.advance_watermark(2).unwrap();
        assert_eq!(log.is_committed(1), Some("committed"));
        assert_eq!(log.is_committed(2), Some("committed"));
    }

    #[test]
    fn test_replication_log_persistence() {
        let tmpfile = tempfile::NamedTempFile::new().unwrap();
        let path = tmpfile.path().to_path_buf();

        // Write some entries
        {
            let store = RedbStore::open(&path).unwrap();
            let log = ReplicationLog::new(&store, 1).unwrap();
            log.append(b"cmd1").unwrap();
            log.append(b"cmd2").unwrap();
            log.advance_watermark(1).unwrap();
        }

        // Reopen and verify state persisted
        {
            let store = RedbStore::open(&path).unwrap();
            let log = ReplicationLog::new(&store, 1).unwrap();
            assert_eq!(log.max_seq(), 3); // next_seq = 3 (two appends)
            assert_eq!(log.is_committed(1), Some("committed"));
            assert_eq!(log.is_committed(2), Some("pending"));
        }
    }

    #[test]
    fn test_drain_unreplicated() {
        let store = RedbStore::open_temporary().unwrap();
        let log = ReplicationLog::new(&store, 42).unwrap();

        log.append(b"cmd1").unwrap();
        log.append(b"cmd2").unwrap();
        log.append(b"cmd3").unwrap();

        // All unreplicated
        let entries = log.drain_unreplicated().unwrap();
        assert_eq!(entries.len(), 3);
        assert_eq!(entries[0].0, 1);
        assert_eq!(entries[0].1.node_id, 42);
        assert_eq!(entries[0].1.command, b"cmd1");

        // Advance watermark to 2
        log.advance_watermark(2).unwrap();
        let entries = log.drain_unreplicated().unwrap();
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].0, 3);
    }

    #[test]
    fn test_compact() {
        let store = RedbStore::open_temporary().unwrap();
        let log = ReplicationLog::new(&store, 1).unwrap();

        // Append 5 entries
        for i in 1..=5 {
            let cmd = format!("cmd{}", i);
            log.append(cmd.as_bytes()).unwrap();
        }
        assert_eq!(log.earliest_seq(), 1);
        assert_eq!(log.max_seq(), 6); // next_seq = 6

        // Compact entries 1-3
        let deleted = log.compact(3).unwrap();
        assert_eq!(deleted, 3);
        assert_eq!(log.earliest_seq(), 4);

        // Verify entries 1-3 are gone from the tree
        for seq in 1..=3u64 {
            assert!(log.log_tree.get(&seq.to_be_bytes()).unwrap().is_none());
        }

        // Verify entries 4-5 still exist
        for seq in 4..=5u64 {
            assert!(log.log_tree.get(&seq.to_be_bytes()).unwrap().is_some());
        }

        // drain_unreplicated still works (watermark=0, so 4 and 5 should appear)
        let entries = log.drain_unreplicated().unwrap();
        // entries start from watermark+1=1, but 1-3 compacted (get returns None, skipped)
        // Actually drain iterates from watermark+1..max, gets None for 1-3, Some for 4-5
        assert_eq!(entries.len(), 2);
        assert_eq!(entries[0].0, 4);
        assert_eq!(entries[1].0, 5);

        // Compact again — idempotent for already-compacted region
        let deleted = log.compact(2).unwrap();
        assert_eq!(deleted, 0);
        assert_eq!(log.earliest_seq(), 4); // unchanged
    }

    #[test]
    fn test_compact_persistence() {
        let tmpfile = tempfile::NamedTempFile::new().unwrap();
        let path = tmpfile.path().to_path_buf();

        // Write entries and compact
        {
            let store = RedbStore::open(&path).unwrap();
            let log = ReplicationLog::new(&store, 1).unwrap();
            log.append(b"cmd1").unwrap();
            log.append(b"cmd2").unwrap();
            log.append(b"cmd3").unwrap();
            log.compact(2).unwrap();
            assert_eq!(log.earliest_seq(), 3);
        }

        // Reopen and verify earliest_seq persisted
        {
            let store = RedbStore::open(&path).unwrap();
            let log = ReplicationLog::new(&store, 1).unwrap();
            assert_eq!(log.earliest_seq(), 3);
            assert_eq!(log.max_seq(), 4); // next_seq preserved

            // Entry 3 still exists
            assert!(log.log_tree.get(&3u64.to_be_bytes()).unwrap().is_some());
            // Entries 1-2 gone
            assert!(log.log_tree.get(&1u64.to_be_bytes()).unwrap().is_none());
            assert!(log.log_tree.get(&2u64.to_be_bytes()).unwrap().is_none());
        }
    }
}
