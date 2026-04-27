//! `CacheStore` ABC — §3 cache pillar (stub).
//!
//! Rust mirror of Python `CacheStoreABC` — the third §3 pillar
//! alongside `ObjectStore` and `MetaStore`. Concrete cache impls today
//! live entirely on the Python side (`nexus.storage.cache.*`); this
//! Rust trait stub anchors the §3 doc invariant ("3 ABC pillars in
//! `rust/kernel/src/abc/`, period") and gives a future-PR home for
//! fleshing out the methods that the Python ABC currently surfaces.
//!
//! Until that work happens the trait is intentionally minimal — the
//! kernel does not yet build any object-safe `Arc<dyn CacheStore>` at
//! runtime, so leaving the surface narrow avoids committing to
//! signatures that may not survive the Rust-side cache impl.

/// Error type for `CacheStore` operations.
///
/// Variants will grow as the methods do; for now a single variant
/// covers the only meaningful failure mode (underlying store I/O).
#[derive(Debug)]
pub enum CacheStoreError {
    IOError(String),
}

/// Cache pillar — kernel cache contract.
///
/// Stub: the abstract surface mirrors Python `CacheStoreABC` but
/// methods are added as the Rust-side cache backends materialise.
/// Today's only known consumers (`nexus.storage.cache.*`) sit fully
/// on the Python side and never cross this trait.
///
/// `Send + Sync` mirrors `MetaStore` / `ObjectStore` — a cache shared
/// across syscall threads must be both.
pub trait CacheStore: Send + Sync {}
