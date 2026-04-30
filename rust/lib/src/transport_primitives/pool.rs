//! Generic TTL-based connection pool.
//!
//! Provides `ConnectionPool<K, V>` — a generic pool that lazily creates
//! connections and evicts entries after a configurable TTL.
//!
//! Used by nexus_raft's `RaftClientPool` as `ConnectionPool<String, RaftClient>`.

use dashmap::DashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

struct PoolEntry<V> {
    value: V,
    last_used: Instant,
}

/// Generic TTL-based connection pool.
///
/// - Lazy: entries created on first `get_or_create` call.
/// - TTL eviction on access (no background thread).
/// - Thread-safe via DashMap.
pub struct ConnectionPool<K, V>
where
    K: std::hash::Hash + Eq + Clone + Send + Sync + 'static,
    V: Send + Sync + 'static,
{
    entries: DashMap<K, PoolEntry<V>>,
    ttl: Duration,
}

impl<K, V> ConnectionPool<K, V>
where
    K: std::hash::Hash + Eq + Clone + Send + Sync + 'static,
    V: Send + Sync + 'static,
{
    /// Create a new pool with the given TTL.
    pub fn new(ttl: Duration) -> Arc<Self> {
        Arc::new(Self {
            entries: DashMap::new(),
            ttl,
        })
    }

    /// Insert or update a connection.
    pub fn insert(&self, key: K, value: V) {
        self.entries.insert(
            key,
            PoolEntry {
                value,
                last_used: Instant::now(),
            },
        );
    }

    /// Remove a connection (e.g., after transport error).
    pub fn remove(&self, key: &K) -> Option<V> {
        self.entries.remove(key).map(|(_, e)| e.value)
    }

    /// Number of active connections.
    pub fn connection_count(&self) -> usize {
        self.entries.len()
    }

    /// Evict all expired entries.
    pub fn evict_expired(&self) {
        self.entries
            .retain(|_, entry| entry.last_used.elapsed() <= self.ttl);
    }
}
