//! `CacheStore` ABC — §3 cache pillar.
//!
//! Rust mirror of Python `CacheStoreABC` — the third §3 pillar
//! alongside `ObjectStore` and `MetaStore`.  Provides ephemeral
//! KV + PubSub storage used by hot caches (permission bitmaps,
//! session state, dcache invalidation events).
//!
//! Concrete impls today live on the Python side
//! (`nexus.storage.cache.*`); this Rust trait is the kernel-side
//! contract that future Rust caches plug into.  The §3 invariant
//! ("3 ABC pillars in `rust/kernel/src/abc/`, period") is anchored
//! here.
//!
//! ## Async shape
//!
//! Trait methods return `Result<T, CacheStoreError>` synchronously.
//! The Python ABC is async, but the kernel-side trait stays sync so
//! that:
//!
//!   - kernel call sites that already hold an executor `Handle` can
//!     `block_on` without trait-object dispatch through `dyn Future`,
//!   - drivers that wrap a sync KV (rocksdb, sled) implement directly,
//!   - drivers that wrap an async client (Dragonfly) own their own
//!     runtime and `block_on` at the trait boundary.
//!
//! `subscribe` returns an opaque iterator handle (`Box<dyn
//! Iterator<Item = Vec<u8>> + Send>`) so each driver picks its
//! delivery primitive (channel, polling, fanout queue) without the
//! trait committing to one.

/// Error type for `CacheStore` operations.
///
/// Variant set mirrors `MetaStoreError` so kernel call sites map
/// store-shape errors uniformly across pillars.
#[derive(Debug)]
pub enum CacheStoreError {
    /// Key not found, or expired before the read.
    NotFound(String),
    /// Pattern is not a legal glob for this driver.
    InvalidPattern(String),
    /// Underlying store I/O / connection error.
    IOError(String),
    /// Driver has been closed and refuses further work.
    Closed,
}

/// Iterator handle returned by [`CacheStore::subscribe`].
///
/// Each item is a single message body delivered on the channel.  The
/// iterator returns `None` when the subscription is dropped (driver
/// closed, channel torn down, etc.).
pub type SubscribeStream = Box<dyn Iterator<Item = Vec<u8>> + Send>;

/// Cache pillar — kernel cache contract.
///
/// `Send + Sync` mirrors `MetaStore` / `ObjectStore` — a cache shared
/// across syscall threads must be both.
pub trait CacheStore: Send + Sync {
    // ─── KV operations ───────────────────────────────────────────────────

    /// Read a value by key.  `Ok(None)` is a cache miss; `Err(_)` is a
    /// driver-side failure the caller treats as "unavailable" rather
    /// than "miss".
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, CacheStoreError>;

    /// Write a key with optional TTL in seconds.  `ttl = None` writes
    /// a non-expiring entry.  Existing keys are overwritten.
    fn set(&self, key: &str, value: &[u8], ttl: Option<u64>) -> Result<(), CacheStoreError>;

    /// Delete a key.  `Ok(true)` if the key existed, `Ok(false)`
    /// otherwise.
    fn delete(&self, key: &str) -> Result<bool, CacheStoreError>;

    /// Check if a key exists and has not expired.
    fn exists(&self, key: &str) -> Result<bool, CacheStoreError>;

    /// Delete every key matching a glob pattern.  Returns the count of
    /// deleted keys.  Pattern syntax: `*` wildcard, same shape as
    /// `fnmatch` / Redis `SCAN MATCH`.
    fn delete_by_pattern(&self, pattern: &str) -> Result<u64, CacheStoreError>;

    /// List every key matching a glob pattern.  Companion to
    /// [`delete_by_pattern`](CacheStore::delete_by_pattern) — same
    /// pattern syntax, returns names instead of deleting.
    fn keys_by_pattern(&self, pattern: &str) -> Result<Vec<String>, CacheStoreError>;

    // ─── PubSub operations ───────────────────────────────────────────────

    /// Publish a message to a channel.  Returns the count of
    /// subscribers that received the message (drivers that cannot
    /// count receivers return `0`).
    fn publish(&self, channel: &str, message: &[u8]) -> Result<u32, CacheStoreError>;

    /// Subscribe to a channel.  Returned [`SubscribeStream`] yields
    /// one message body per item; iteration ends when the
    /// subscription is dropped.
    fn subscribe(&self, channel: &str) -> Result<SubscribeStream, CacheStoreError>;

    // ─── Lifecycle ───────────────────────────────────────────────────────

    /// Probe driver health.  `Ok(true)` = backend is responsive,
    /// `Ok(false)` = backend is degraded but reachable, `Err(_)` =
    /// unreachable.
    fn health_check(&self) -> Result<bool, CacheStoreError>;

    /// Release driver resources.  After `close()` further calls return
    /// [`CacheStoreError::Closed`].
    fn close(&self) -> Result<(), CacheStoreError>;
}
