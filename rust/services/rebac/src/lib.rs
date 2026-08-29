//! `nexus-rebac` — the Rust replacement for `src/nexus/bricks/
//! rebac/` (relationship-based access control, Zanzibar-shaped).
//!
//! Part of the R10 pure-Rust `nexus-server` migration (epic
//! #4674).  Ships in phases:
//!
//!   * **PR-1 (this crate skeleton)** — [`store::ReBACTupleStore`]
//!     HAL trait + [`store::NoopReBACTupleStore`] fail-closed
//!     default + [`inmem::InMemoryReBACTupleStore`] real impl.
//!     No enforcer, no graph cache, no HTTP wire, no kernel hook.
//!     Sets the seam other crates code against.
//!   * PR-2 — `RaftReBACTupleStore` (a `raft::ControlStateStore`
//!     wrapper with namespace `"rebac"`) + graph cache built on
//!     `lib::rebac::ReBACGraph`.
//!   * PR-3 — `RebacPermissionProvider` impl of
//!     `kernel::PermissionProvider` + composition-root wire in
//!     `nexusd`.
//!   * PR-4 — HTTP `/v2/rebac/tuples` router (grant / list /
//!     revoke) + post-filter wire in `handlers::search` +
//!     `handlers::documents`.
//!   * PR-5 — Delete `src/nexus/bricks/rebac/` (~37k LoC Python).
//!
//! # SSOT — kernel metastore, not Postgres
//!
//! Deliberately NOT the Python side's Postgres schema.  Nexus's
//! Rust `ApiKeyAuthProvider` already migrated OFF Postgres onto
//! `KernelSlotStore` (raft-replicated); ReBAC follows the same
//! posture.  Rationale:
//!
//!   * Rust binary stays self-contained (no PG deploy dep).
//!   * Cross-node consistency is raft's problem, not the caller's
//!     (matches the standing `feedback_distributed_ssot` rule).
//!   * One SSOT shape across auth + rebac control-plane state.
//!
//! # Values are opaque bytes
//!
//! The store is a generic key/value primitive; the enforcer (PR-3)
//! owns the tuple encoding.  Same "the store never interprets the
//! value" contract as upstream `AuthKeyStore`.  Keeps the store
//! reusable across record kinds if a future ReBAC feature adds
//! namespace configs / zone-revision blobs / etc.

pub mod inmem;
pub mod store;

// Re-export the trait + error at the crate root so callers reach
// them at one path (`nexus_rebac::ReBACTupleStore`) instead of
// remembering the module layout — same convention as
// `nexus_http_api::AppState`.
pub use inmem::InMemoryReBACTupleStore;
pub use store::{NoopReBACTupleStore, ReBACTupleStore, ReBACTupleStoreError};
