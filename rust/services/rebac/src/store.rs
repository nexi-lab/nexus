//! The `ReBACTupleStore` HAL trait — the seam every callable ReBAC
//! surface (enforcer, HTTP grant router, kernel `PermissionProvider`
//! impl) reads from.
//!
//! # Why a trait, not a concrete
//!
//! Three impls at different tiers:
//!
//!   * [`NoopReBACTupleStore`] — the fail-closed default installed at
//!     boot, before the raft-backed store exists (matches the
//!     `NoopAuthKeyStore` posture upstream).
//!   * [`crate::inmem::InMemoryReBACTupleStore`] — test double + dev
//!     backend when no cluster is configured.
//!   * `RaftReBACTupleStore` (PR-2, not in this crate yet) — the
//!     production impl, a thin wrapper over
//!     `raft::ControlStateStore` with namespace `"rebac"`.
//!
//! Callers hold `Arc<dyn ReBACTupleStore>` and never name a concrete —
//! same DI shape as `kernel::hal::auth_key_store::AuthKeyStore`
//! upstream.
//!
//! # Values are opaque bytes
//!
//! The store never interprets the value; the enforcer owns the
//! schema (Zanzibar tuple bytes today; a namespace-config blob or
//! zone-revision counter tomorrow — one store surface, many
//! callers).  Matches the `AuthKeyStore` "values are opaque"
//! rationale — keeps the store generic across ReBAC record kinds.
//!
//! # Sync by design
//!
//! Every caller (the enforcer running in the kernel `PermissionProvider`
//! slot; the http-api handlers under `axum::extract::State`) reaches
//! this on the syscall / request hot path.  Async would force a
//! `runtime.spawn` on every check — the impl bridges to any async
//! backend internally (raft's `propose` becomes a `block_on_via`
//! inside `RaftReBACTupleStore`).

use std::error::Error;
use std::fmt;

/// Failure to reach the tuple store, or to commit a write to it.
///
/// One variant on purpose: every caller treats an error the same
/// way — **fail closed**.  A check that cannot read the store must
/// deny the permission rather than guess, and a grant tool that
/// cannot commit a `put` / `delete` must report the write as not
/// durable.
#[derive(Debug, thiserror::Error)]
pub enum ReBACTupleStoreError {
    /// The store could not be read, or the write was not committed
    /// (consensus rejected the proposal, this node is not the
    /// leader, or the underlying storage failed).  Carries the
    /// backend's message for the operator log.
    #[error("rebac tuple store backend error: {0}")]
    Backend(String),
}

/// Distinct owned-error type for cross-crate propagation — same
/// shape as `AuthKeyStore`'s `Box<dyn Error + Send + Sync>` bound
/// for the underlying source. Kept simple (one variant) so callers
/// do not need a match.
impl ReBACTupleStoreError {
    /// Wrap any error into the single `Backend` variant.  Used by
    /// impls that adapt a foreign error type (raft errors, redb
    /// errors, sqlx errors in the deprecated PG path).
    pub fn backend<E: Error + Send + Sync + 'static>(err: E) -> Self {
        ReBACTupleStoreError::Backend(err.to_string())
    }
}

/// The kernel-adjacent store of Zanzibar tuples.
///
/// # Contract
///
/// * `put` is idempotent — writing the same `(key, value)` twice
///   MUST NOT double-count.  Impls that back a log-ordered raft
///   proposal accept this by convention (a repeated put commits
///   as a single revision bump; see [`Self::zone_revision`]).
///
/// * `delete` is idempotent — deleting a missing key returns
///   `Ok(false)`, not an error.  Callers `assert!(returned || not_
///   present)`, they don't retry on false.
///
/// * `list` returns the FULL snapshot for the current namespace —
///   there is no pagination.  Real deployments cap ReBAC tuples at
///   O(100k) per zone (matches the Python Tiger/Leopard-cached
///   design point); a full-scan is O(ms) and rebuilds the
///   in-memory graph cache in one shot.
///
/// * `zone_revision(zone)` is a monotonic counter the enforcer
///   uses as a cache-freshness key: on `put`/`delete` for a given
///   zone the counter bumps; readers that saw revision N can skip
///   the full-scan rebuild as long as the current revision is
///   still N.  A store that cannot cheaply track this returns
///   `0` (equivalent to "always stale — always rebuild") — safe,
///   just slower.
pub trait ReBACTupleStore: Send + Sync {
    /// Write `value` at `key`.  Idempotent — same-value writes
    /// are no-ops at the storage layer, though they may bump the
    /// zone revision.  Returns Err only on storage failure, never
    /// on "already exists."
    fn put(&self, key: &str, value: &[u8]) -> Result<(), ReBACTupleStoreError>;

    /// Delete `key`.  Returns `Ok(true)` when the key existed,
    /// `Ok(false)` when it did not — idempotent; a delete on a
    /// missing key is not an error.
    fn delete(&self, key: &str) -> Result<bool, ReBACTupleStoreError>;

    /// Read the value at `key`, or `Ok(None)` when the key is
    /// absent.
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, ReBACTupleStoreError>;

    /// Full snapshot of the store's contents.  Callers rebuild the
    /// per-zone `lib::rebac::ReBACGraph` from this on cache miss;
    /// see the trait doc for the size-budget rationale.
    fn list(&self) -> Result<Vec<(String, Vec<u8>)>, ReBACTupleStoreError>;

    /// Monotonic per-zone revision counter — the cache-freshness
    /// key the enforcer's graph cache reads to decide whether it
    /// can skip the full-scan rebuild.
    ///
    /// An impl that cannot cheaply track this returns `0` (always-
    /// stale — safe but slower).  The default here is `0`, so a
    /// minimal impl only needs to implement the 4 CRUD methods.
    fn zone_revision(&self, _zone: &str) -> Result<u64, ReBACTupleStoreError> {
        Ok(0)
    }
}

/// Fail-closed default installed at boot before the real store
/// exists.  Every method is a no-op: `list` returns empty (so the
/// enforcer's graph is empty ⇒ no permissions granted ⇒ every
/// check denies), `put`/`delete` succeed silently (so a grant
/// tool that fires before the raft store is up records the write
/// but does not error — the write is lost, matching the "boot
/// order safety, real store swaps in when ready" pattern of
/// upstream `NoopAuthKeyStore`).
///
/// **NEVER install this in a real deployment** — the enforcer
/// backed by this will deny every permission check.  Compose the
/// real backend before the http-api service starts serving.
///
/// The one-shot `list` returning empty is the SAFE fail-closed
/// posture: a live enforcer reading an empty graph denies every
/// non-admin request, which surfaces the mis-wiring loudly (403 /
/// 401 chain) rather than silently admitting requests under a
/// missing store.
pub struct NoopReBACTupleStore;

impl ReBACTupleStore for NoopReBACTupleStore {
    fn put(&self, _key: &str, _value: &[u8]) -> Result<(), ReBACTupleStoreError> {
        Ok(())
    }

    fn delete(&self, _key: &str) -> Result<bool, ReBACTupleStoreError> {
        Ok(false)
    }

    fn get(&self, _key: &str) -> Result<Option<Vec<u8>>, ReBACTupleStoreError> {
        Ok(None)
    }

    fn list(&self) -> Result<Vec<(String, Vec<u8>)>, ReBACTupleStoreError> {
        Ok(Vec::new())
    }
}

/// Diagnostic Debug impl — `Arc<dyn ReBACTupleStore>` shows up in
/// service-state Debug output; a bare "NoopReBACTupleStore" tells
/// an operator immediately why every check denies.
impl fmt::Debug for NoopReBACTupleStore {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "NoopReBACTupleStore")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn noop_put_get_returns_none() {
        let s = NoopReBACTupleStore;
        s.put("k", b"v").expect("put ok");
        assert_eq!(s.get("k").expect("get ok"), None);
    }

    #[test]
    fn noop_delete_returns_false_on_missing_key() {
        // Idempotent delete — a caller that retries a delete never
        // sees Err on the "already gone" branch.
        let s = NoopReBACTupleStore;
        assert!(!s.delete("k").expect("delete ok"));
    }

    #[test]
    fn noop_list_returns_empty_vec() {
        // The fail-closed posture — enforcer builds an empty graph,
        // every non-admin check denies.  A test asserting >0 here
        // would surface a regression that made Noop silently admit
        // wrong tuples.
        let s = NoopReBACTupleStore;
        assert!(s.list().expect("list ok").is_empty());
    }

    #[test]
    fn noop_zone_revision_defaults_to_zero() {
        // 0 = "always stale" = safe fail-closed default for the
        // freshness key.  Real impls override.
        let s = NoopReBACTupleStore;
        assert_eq!(s.zone_revision("root").expect("revision ok"), 0);
    }

    #[test]
    fn error_backend_wraps_arbitrary_source() {
        // Impls adapting foreign error types use `backend()` to
        // preserve the source's Display in the operator log.
        let src = std::io::Error::new(std::io::ErrorKind::PermissionDenied, "eaccess");
        let e = ReBACTupleStoreError::backend(src);
        assert!(e.to_string().contains("eaccess"));
    }
}
