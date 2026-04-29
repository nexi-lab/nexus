//! `ObjectStoreProvider` HAL trait — Control-Plane HAL §3.B.2.
//!
//! `sys_setattr`'s 17-way backend-type construction switch (OpenAI,
//! Anthropic, S3, GCS, …) needs to instantiate concrete `ObjectStore`
//! impls without the kernel naming `backends::*` (which would close
//! the kernel ↔ backends Cargo cycle). Kernel declares this trait +
//! a `OnceLock<Arc<dyn ObjectStoreProvider>>` slot; the concrete impl
//! ([`backends::python::factory::DefaultObjectStoreProvider`]) lives
//! in the `backends` crate, and `nexus-cdylib`'s `#[pymodule]` boot
//! registers it before any `sys_setattr` call fires. Same DI shape as
//! the §3.B.1 [`DistributedCoordinator`](super::distributed_coordinator::DistributedCoordinator).
//!
//! ## Args struct
//!
//! [`ObjectStoreProviderArgs`] bundles every parameter `sys_setattr`
//! accepts that a backend constructor might consume — 30+ fields,
//! mostly `Option<&str>`. Borrowed lifetimes match the `sys_setattr`
//! PyO3 method's argument lifetimes so callers skip per-arg `String`
//! allocation on the hot path.

use std::sync::{Arc, OnceLock};

use crate::abc::object_store::ObjectStore;
use crate::cas_remote::RemoteChunkFetcher;
use crate::hal::peer::PeerBlobClient;
use crate::meta_store::MetaStore;

/// Bundle of every parameter a backend constructor might consume.
///
/// Matches the union of all `sys_setattr` named-args that flow into
/// `Backend*::new(...)` calls.  Borrowed lifetimes track the
/// `sys_setattr` PyO3 args so no per-call allocation is needed.
#[allow(missing_docs)]
pub struct ObjectStoreProviderArgs<'a> {
    pub backend_type: &'a str,
    pub backend_name: &'a str,
    pub local_root: Option<&'a str>,
    pub fsync: bool,
    pub follow_symlinks: bool,
    pub openai_base_url: Option<&'a str>,
    pub openai_api_key: Option<&'a str>,
    pub openai_model: Option<&'a str>,
    pub openai_blob_root: Option<&'a str>,
    pub anthropic_base_url: Option<&'a str>,
    pub anthropic_api_key: Option<&'a str>,
    pub anthropic_model: Option<&'a str>,
    pub anthropic_blob_root: Option<&'a str>,
    pub s3_bucket: Option<&'a str>,
    pub s3_prefix: Option<&'a str>,
    pub aws_region: Option<&'a str>,
    pub aws_access_key: Option<&'a str>,
    pub aws_secret_key: Option<&'a str>,
    pub s3_endpoint: Option<&'a str>,
    pub gcs_bucket: Option<&'a str>,
    pub gcs_prefix: Option<&'a str>,
    pub access_token: Option<&'a str>,
    pub root_folder_id: Option<&'a str>,
    pub bot_token: Option<&'a str>,
    pub default_channel: Option<&'a str>,
    pub hn_stories_per_feed: Option<usize>,
    pub hn_include_comments: Option<bool>,
    pub cli_command: Option<&'a str>,
    pub cli_service: Option<&'a str>,
    pub cli_auth_env_json: Option<&'a str>,
    pub x_bearer_token: Option<&'a str>,
    pub server_address: Option<&'a str>,
    pub remote_auth_token: Option<&'a str>,
    pub remote_ca_pem: Option<&'a [u8]>,
    pub remote_cert_pem: Option<&'a [u8]>,
    pub remote_key_pem: Option<&'a [u8]>,
    pub remote_timeout: f64,
    /// Shared `peer_blob_client::PeerBlobClient` — needed by the LLM
    /// connector backends (anthropic / openai) so streaming SSE
    /// responses can land in the kernel CAS via shared transport.
    pub peer_client: &'a Arc<dyn PeerBlobClient>,
    /// Shared scatter-gather chunk fetcher.  Pre-wired into the
    /// `CasLocalBackend` constructor so chunk misses on this mount
    /// fall through to peer RPCs against `backend_name.origins`.
    pub chunk_fetcher: Arc<dyn RemoteChunkFetcher>,
    /// Kernel's tokio runtime — backends that issue async network IO
    /// (anthropic / openai SSE, RPC transport for remote backends)
    /// share this runtime instead of building their own. The HAL
    /// `PeerBlobClient` trait is sync-only, so runtime ownership stays
    /// with the kernel struct and gets threaded through here for the
    /// rare async-needing backends.
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
    /// Backend instance, or `None` when `args.backend_type` is one
    /// of the kernel-side defaults (`""`, `"path_local"`,
    /// `"local_connector"`, `"cas-local"`) that this factory leaves
    /// to the kernel to construct directly via the
    /// `_backend_impls`-equivalent ObjectStore impls in
    /// `backends::storage::*`.
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
/// register attempts (returns `Err(existing)`). Called once at
/// `nexus-cdylib`'s `#[pymodule]` boot before Python can invoke
/// `sys_setattr`.
pub fn set_provider(
    provider: Arc<dyn ObjectStoreProvider>,
) -> Result<(), Arc<dyn ObjectStoreProvider>> {
    OBJECT_STORE_PROVIDER.set(provider)
}

/// Read the registered provider. Returns `None` until a caller
/// registers one — `sys_setattr` surfaces that as a runtime error
/// rather than panicking, so non-cdylib Rust tests can wire up their
/// own provider before exercising mounts.
pub fn get_provider() -> Option<Arc<dyn ObjectStoreProvider>> {
    OBJECT_STORE_PROVIDER.get().cloned()
}
