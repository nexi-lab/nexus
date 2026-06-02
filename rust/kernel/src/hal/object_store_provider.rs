//! `ObjectStoreProvider` HAL trait — Control-Plane HAL §3.B.2.
//!
//! `sys_setattr`'s backend-type construction switch needs to
//! instantiate concrete `ObjectStore` impls without the kernel naming
//! `backends::*` (which would close the kernel ↔ backends Cargo
//! cycle). Kernel declares this trait + a
//! `OnceLock<Arc<dyn ObjectStoreProvider>>` slot; the concrete impl
//! lives in the `backends` crate and is registered by the host
//! binary (e.g. `profiles::cluster`) before any `sys_setattr` call
//! fires. Same DI shape as the §3.B.1
//! [`DistributedCoordinator`](super::distributed_coordinator::DistributedCoordinator).
//!
//! ## Args struct
//!
//! [`ObjectStoreProviderArgs`] carries the dispatch key
//! (`backend_type`), an opaque `backend_params` map for app-layer
//! constructor params, and shared kernel infra refs (`peer_client`,
//! `runtime`). Each backend arm in the provider parses its own
//! required keys from the map — the kernel HAL never names
//! individual app-layer params.

use std::collections::HashMap;
use std::sync::{Arc, OnceLock};

use crate::abc::object_store::ObjectStore;
use crate::hal::peer::PeerBlobClient;
use crate::meta_store::MetaStore;

/// Bundle of parameters for backend construction.
///
/// `backend_type` is the dispatch key; `backend_params` carries every
/// app-layer constructor param as opaque key-value strings.  Each
/// provider arm parses its own required keys (e.g. `s3_bucket`,
/// `aws_region`) from the map. The kernel HAL never names individual
/// params — this keeps the kernel ↔ app-layer boundary clean.
///
/// Infra refs (`peer_client`, `runtime`) are shared kernel resources
/// threaded through for backends that need async IO or peer RPCs.
pub struct ObjectStoreProviderArgs<'a> {
    /// Dispatch key: `"s3"`, `"gcs"`, `"remote"`, `"path_local"`, etc.
    pub backend_type: &'a str,
    /// Logical name for the mount (e.g. `"r2-prod"`, `"cas-data"`).
    pub backend_name: &'a str,
    /// Local mount point (`sys_setattr`'s `path`). The `remote` arm
    /// uses it to fail closed on sub-path mounts (#4273).
    pub mount_path: Option<&'a str>,
    /// Opaque app-layer params. Each backend arm parses its own
    /// required keys. String values; binary data (PEM) is stored as
    /// UTF-8 text and converted by the consumer.
    pub backend_params: &'a HashMap<String, String>,
    /// Shared `PeerBlobClient` for backends needing peer RPCs.
    pub peer_client: &'a Arc<dyn PeerBlobClient>,
    /// This node's advertised address (for scatter-gather skip-self).
    pub self_address: Option<&'a str>,
    /// Kernel's tokio runtime for backends with async IO.
    pub runtime: &'a Arc<tokio::runtime::Runtime>,
}

/// Result of a backend construction.
///
/// Some backend types (`"remote"`) need to side-effect a kernel
/// `pending_remote_meta_store` slot in addition to producing the
/// `ObjectStore` — they wrap an RPC transport that backs both the
/// metastore and the object store.  The factory bundles both pieces
/// here; `Kernel::sys_setattr` consumes them separately (object
/// store goes on the mount entry, optional metastore goes on the
/// kernel's pending slot for the next `add_mount`).
pub struct ObjectStoreBuildResult {
    /// Backend instance. `Option` so a provider impl *may* signal "no
    /// backend installed for this mount" (e.g. a `sys_setattr` setting
    /// metadata only) by returning `Ok` with `None`; callers must treat
    /// `None` as that case. The in-tree `DefaultObjectStoreProvider`
    /// (backends crate) does not use that path — it always returns
    /// `Some` on `Ok`, and surfaces an unknown `backend_type` or a
    /// missing required arg as `Err` rather than `Ok(None)`.
    pub backend: Option<Arc<dyn ObjectStore>>,
    /// `Some` only for `backend_type = "remote"`: the
    /// `RemoteMetaStore` wrapping the same `RpcTransport` as the
    /// returned `RemoteBackend`.  Kernel installs it via
    /// `pending_remote_meta_store`.
    pub pending_remote_meta_store: Option<Arc<dyn MetaStore>>,
}

/// Build a concrete `ObjectStoreBuildResult` from a `ObjectStoreProviderArgs`.
///
/// Returns `Ok` with a possibly-empty result on success and
/// `Err(message)` for construction failures (missing required arg,
/// I/O error initialising the local CAS dir, etc.).
///
/// `Send + Sync` so the registered factory can be shared across
/// syscall threads.
pub trait ObjectStoreProvider: Send + Sync {
    fn build(&self, args: &ObjectStoreProviderArgs<'_>) -> Result<ObjectStoreBuildResult, String>;
}

static OBJECT_STORE_PROVIDER: OnceLock<Arc<dyn ObjectStoreProvider>> = OnceLock::new();

/// Register the global `ObjectStoreProvider`. Idempotent on duplicate
/// register attempts (returns `Err(existing)`). Called once by the
/// host binary at startup, before any `sys_setattr(DT_MOUNT)` fires.
pub fn set_provider(
    provider: Arc<dyn ObjectStoreProvider>,
) -> Result<(), Arc<dyn ObjectStoreProvider>> {
    OBJECT_STORE_PROVIDER.set(provider)
}

/// Read the registered provider. Returns `None` until a caller
/// registers one — `sys_setattr` surfaces that as a runtime error
/// rather than panicking, so Rust tests can wire up their own
/// provider before exercising mounts.
pub fn get_provider() -> Option<Arc<dyn ObjectStoreProvider>> {
    OBJECT_STORE_PROVIDER.get().cloned()
}
