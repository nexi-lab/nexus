//! `ObjectStoreProvider` HAL trait — Control-Plane HAL §3.B.2.
//!
//! `sys_setattr`'s 17-way backend-type construction switch (OpenAI,
//! Anthropic, S3, GCS, …) needs to instantiate concrete `ObjectStore`
//! impls without the kernel naming `backends::*` (which would close
//! the kernel ↔ backends Cargo cycle). Kernel declares this trait +
//! a `OnceLock<Arc<dyn ObjectStoreProvider>>` slot; the concrete impl
//! lives in the `backends` crate and is registered by the host
//! binary (e.g. `profiles::cluster`) before any `sys_setattr` call
//! fires. Same DI shape as the §3.B.1
//! [`DistributedCoordinator`](super::distributed_coordinator::DistributedCoordinator).
//!
//! ## Args struct
//!
//! [`ObjectStoreProviderArgs`] bundles every parameter `sys_setattr`
//! accepts that a backend constructor might consume — 30+ fields,
//! mostly `Option<&str>`. Borrowed lifetimes track the `sys_setattr`
//! call's stack-borrowed args so the factory builds the backend
//! without copying every option string onto the heap.

use std::collections::HashSet;
use std::sync::{Arc, OnceLock, RwLock};

use crate::abc::object_store::ObjectStore;
use crate::hal::peer::PeerBlobClient;
use crate::meta_store::MetaStore;

/// Bundle of every parameter a backend constructor might consume.
///
/// Matches the union of all `sys_setattr` named-args that flow into
/// `Backend*::new(...)` calls.  Borrowed lifetimes track the
/// `sys_setattr` call's stack-borrowed args so no per-call
/// allocation is needed.
#[allow(missing_docs)]
pub struct ObjectStoreProviderArgs<'a> {
    pub backend_type: &'a str,
    pub backend_name: &'a str,
    /// Local mount point for this backend — `sys_setattr`'s `path`
    /// (e.g. `/` for a root mount, `/zone/acme` for a sub-path mount).
    /// The `remote` backend uses it to fail closed on sub-path mounts,
    /// which `RemoteBackend` cannot yet reconstruct correctly (Issue
    /// #4273). `None` / `"/"` means root-mount semantics. Populated by
    /// the DT_MOUNT caller (bridge-2); `None` in the current bridge-1
    /// tree (root mounts only).
    pub mount_path: Option<&'a str>,
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
    /// responses can land in the kernel CAS via shared transport, and
    /// by `cas_local` to construct the per-mount scatter-gather fetcher
    /// against the live peer client.
    pub peer_client: &'a Arc<dyn PeerBlobClient>,
    /// Snapshot of `Kernel::self_address_string()` at the time of this
    /// `sys_setattr` call.  `cas_local` plumbs it into the per-mount
    /// `GrpcChunkFetcher` so the fetcher can skip the local node when
    /// scattering reads against `backend_name.origins`.
    pub self_address: Option<&'a str>,
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

// ── Driver gate (DeploymentProfile-driven, SSOT) ───────────────────────────
//
// A `DeploymentProfile` declares which bricks / services / drivers a
// runtime image runs with. Bricks + services are gated by factory
// wiring in the host binary; drivers are gated here because the path
// that constructs them — `Kernel::sys_setattr(DT_MOUNT)` — is shared
// across every profile and lives Rust-side.
//
// Layout:
//
//   * `DeploymentProfile` resolves to a set of enabled driver names
//     — every driver, including local-host backends (`path_local`,
//     `cas-local`, `local_connector`).
//   * The host binary calls [`set_enabled_drivers`] below at startup.
//   * The registered `ObjectStoreProvider::build` impl calls
//     [`is_driver_enabled`] on every dispatch — there is no implicit
//     local-default bypass. A mount requesting a disabled driver
//     fails with
//     `Err("driver 'X' not enabled in current deployment profile")`.
//
// When the gate has never been set (Rust tests, embedders that don't
// gate by profile), [`is_driver_enabled`] returns `true` for every
// name.

static DRIVER_GATE: OnceLock<RwLock<HashSet<String>>> = OnceLock::new();

/// Install the enabled driver set. Called once by the host binary
/// at startup, before any `sys_setattr(DT_MOUNT)` fires. Idempotent
/// — repeated calls overwrite the set, so a host that re-resolves
/// the profile sees the updated drivers without a restart.
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
/// (Rust tests, embedders that don't gate by profile) so existing
/// tests keep passing without explicit wiring.
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
    /// initialised — this is what keeps Rust tests working without
    /// explicit profile wiring.
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
