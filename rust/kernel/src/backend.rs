//! `crate::backend` — flat re-export shim.
//!
//! Phase B split the ObjectStore trait declaration from its three
//! kernel-internal impls (`CasLocalBackend`, `PathLocalBackend`,
//! `LocalConnectorBackend`). The traits + types now live in
//! `crate::abc::object_store`; the impls live in
//! `crate::_backend_impls` (a holding pen until Phase D lifts them
//! into the parallel `backends/` crate).
//!
//! This module re-exports both so existing `use crate::backend::{ObjectStore,
//! StorageError, WriteResult, CasLocalBackend, …}` imports keep working
//! across Phase B / C without a 17-file caller churn. Phase D removes
//! this shim once the impls move out.

// `_backend_impls` items are `pub(crate)` (CasLocalBackend, PathLocalBackend,
// LocalConnectorBackend) so the re-export visibility must match. Trait + types
// stay `pub` because Phase D's `backends/` crate will pull them across the
// crate boundary.
pub(crate) use crate::_backend_impls::*;
pub use crate::abc::object_store::{ExternalTransport, ObjectStore, StorageError, WriteResult};
