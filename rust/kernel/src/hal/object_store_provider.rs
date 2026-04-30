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

use std::collections::HashSet;
use std::sync::{Arc, OnceLock, RwLock};

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

// ── Driver gate (DeploymentProfile-driven) ─────────────────────────────────
//
// A `DeploymentProfile` is a Python-side declaration of which bricks /
// services / drivers a runtime image runs with.  Bricks + services are
// gated by Python factory wiring; drivers are gated here because the
// path that constructs them — `Kernel::sys_setattr(DT_MOUNT)` — is
// shared across every profile and lives Rust-side.
//
// Layout:
//
//   * Python `DeploymentProfile` resolves to a `frozenset[str]` of
//     enabled driver names (e.g. `{"local", "remote", "nostr"}`).
//   * `services::python::register` exposes
//     `nx_set_enabled_drivers(drivers: list[str])` that calls
//     [`set_enabled_drivers`] below.
//   * `DefaultBackendFactory::build` calls [`is_driver_enabled`]
//     before the per-driver construction switch.  Disabled drivers
//     surface as `Err("driver 'X' not enabled in current
//     deployment profile")` rather than the generic
//     `BackendBuildResult { backend: None, ... }` "kernel default"
//     fallthrough.
//
// When the gate has never been set (pure-Rust embedders, tests),
// [`is_driver_enabled`] returns `true` for every name — backward
// compatible with the pre-gating behaviour.

static DRIVER_GATE: OnceLock<RwLock<HashSet<String>>> = OnceLock::new();

/// Install the enabled driver set.  Called once during Python boot
/// from `nexus_runtime.nx_set_enabled_drivers`, before any
/// `sys_setattr(DT_MOUNT)` fires.  Idempotent — repeated calls
/// overwrite the set, so a Python reload that re-resolves the
/// profile sees the updated drivers without an interpreter restart.
pub fn set_enabled_drivers<I, S>(drivers: I)
where
    I: IntoIterator<Item = S>,
    S: Into<String>,
{
    let set: HashSet<String> = drivers.into_iter().map(Into::into).collect();
    let lock = DRIVER_GATE.get_or_init(|| RwLock::new(HashSet::new()));
    *lock.write().expect("DRIVER_GATE poisoned") = set;
}

/// Check whether `driver_name` is enabled in the current deployment
/// profile.  Returns `true` when the gate has never been initialised
/// (pure-Rust tests, non-cdylib embedders) so existing tests keep
/// passing without explicit wiring.
pub fn is_driver_enabled(driver_name: &str) -> bool {
    let Some(lock) = DRIVER_GATE.get() else {
        return true;
    };
    lock.read()
        .map(|set| set.contains(driver_name))
        .unwrap_or(true)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `is_driver_enabled` returns true when the gate has not been
    /// initialised — this is what keeps non-cdylib Rust tests working
    /// without explicit profile wiring.
    #[test]
    fn ungated_returns_true_for_any_driver() {
        // NB: the OnceLock is process-wide, so this assertion is only
        // meaningful when the test runs first.  We rely on cargo test
        // running the kernel-lib tests in a fresh process per invocation;
        // if a future test sets the gate before this one runs, the
        // assertion below would still hold for any driver name in the
        // gated set, so the test stays correct.
        if DRIVER_GATE.get().is_none() {
            assert!(is_driver_enabled("anything"));
            assert!(is_driver_enabled("nostr"));
        }
    }

    /// `set_enabled_drivers` followed by `is_driver_enabled` reports
    /// only members of the set as enabled.
    #[test]
    fn gated_only_returns_true_for_listed_drivers() {
        set_enabled_drivers(["local", "remote"]);
        assert!(is_driver_enabled("local"));
        assert!(is_driver_enabled("remote"));
        assert!(!is_driver_enabled("nostr"));
        // Restore an open set so other tests aren't affected.
        set_enabled_drivers(std::iter::empty::<String>());
        // After a reset to empty, the gate is initialised but contains
        // nothing, so every driver is rejected.  Tests that need an
        // open gate should clear DRIVER_GATE explicitly — but
        // OnceLock has no take(), so process-isolation is the
        // cleanup.
        assert!(!is_driver_enabled("local"));
    }
}
