//! `nexus-vault` — password-vault dylib plugin.
//!
//! Loaded by `nexusd-cluster` at startup via `--plugin-dir`. Registers
//! as a `RustService` named `"password-vault"` through the plugin ABI's
//! `declare_service_plugin!` macro.
//!
//! Data layout (under `$NEXUS_DATA_DIR/vault/`):
//!   vault/vault-meta.redb  — private kernel metastore (entry metadata)
//!   vault/content/         — PathLocalBackend (encrypted entry blobs)
//!   vault/master.key       — 32-byte AES-256 master key
//!                            (auto-generated on first load)
//!
//! Unified storage layout (under `/vault` kernel mount):
//!   /vault/entries/passwords/{title}           → SecretIndex
//!   /vault/entries/{namespace}/{key}            → SecretIndex
//!   /vault/versions/passwords/{title}/{v:010}   → StoredEntry
//!   /vault/versions/{namespace}/{key}/{v:010}   → StoredEntry

pub mod idle;

use std::ffi::c_char;
use std::path::PathBuf;
use std::sync::Arc;

use nexus_plugin_abi::{declare_service_plugin, KernelHandle};
use prost::Message;
use tonic::Request;

use services::password_vault::proto::password_vault_service_server::PasswordVaultService;
use services::password_vault::proto::*;
use services::password_vault::PasswordVaultServiceImpl;

use services::generic_secrets::proto::generic_secrets_service_server::GenericSecretsService;
use services::generic_secrets::GenericSecretsServiceImpl;

// Alias to disambiguate from password_vault proto types.
mod secrets_proto {
    pub use services::generic_secrets::proto::*;
}

/// Plugin state: the vault service plus a single-threaded tokio runtime
/// for driving the async gRPC trait methods.
struct VaultPlugin {
    svc: PasswordVaultServiceImpl,
    secrets_svc: GenericSecretsServiceImpl,
    rt: tokio::runtime::Runtime,
}

fn create_vault(_kernel_handle: &KernelHandle) -> Box<VaultPlugin> {
    let data_dir = std::env::var("NEXUS_DATA_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("./nexus-data"));
    let vault_dir = data_dir.join("vault");
    std::fs::create_dir_all(&vault_dir).expect("create vault data dir");

    let kernel = Arc::new(kernel::kernel::Kernel::new());
    let meta_path = vault_dir.join("vault-meta.redb");
    if let Some(p) = meta_path.to_str() {
        kernel
            .set_metastore_path(p)
            .expect("set vault metastore path");
    }

    let content_dir = vault_dir.join("content");
    let backend: Arc<dyn kernel::abc::object_store::ObjectStore> = Arc::new(
        backends::storage::path_local::PathLocalBackend::new(&content_dir, /* fsync */ true)
            .expect("create PathLocalBackend for vault content"),
    );
    let backend_name = backend.name().to_string();

    // Mount backend at /vault — shared by both services.
    kernel
        .sys_setattr(
            "/vault",
            /* DT_MOUNT */ 2,
            &backend_name,
            Some(backend),
            None,
            None,
            "memory",
            "root",
            false,
            0,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        .expect("mount vault backend");

    let master_key_path = vault_dir.join("master.key");
    let master_key = services::password_vault::crypto::load_or_create_master_key(&master_key_path)
        .expect("load/create master key");

    // GenericSecretsService owns the unified storage layer.
    let secrets_svc =
        GenericSecretsServiceImpl::new_on_existing_mount(kernel, "/vault", master_key)
            .expect("open generic secrets");

    // PasswordVaultService wraps GenericSecretsService (namespace="passwords").
    let svc = PasswordVaultServiceImpl::new_with_secrets(secrets_svc.clone());

    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("build tokio runtime for vault plugin");

    tracing::info!(
        vault_dir = %vault_dir.display(),
        "vault plugin loaded (password-vault + generic-secrets)"
    );

    Box::new(VaultPlugin {
        svc,
        secrets_svc,
        rt,
    })
}

fn dispatch_vault(plugin: &VaultPlugin, method: &str, payload: &[u8]) -> Result<Vec<u8>, i32> {
    // Phase P bytes-level gRPC routing. Methods starting with '/' are
    // full gRPC paths forwarded by `nexus-vfs` `PluginProxyService` —
    // translate to the legacy short-name arms below so there is exactly
    // one match table per concern.
    if method.starts_with('/') {
        return dispatch_grpc(plugin, method, payload);
    }
    match method {
        "put_entry" => {
            let req = PutEntryRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.put_entry(Request::new(req)))
                .map_err(|_| -3)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "get_entry" => {
            let req = GetEntryRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.get_entry(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "list_entries" => {
            let req = ListEntriesRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.list_entries(Request::new(req)))
                .map_err(|_| -3)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "delete_entry" => {
            let req = DeleteEntryRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.delete_entry(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "restore_entry" => {
            let req = RestoreEntryRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.restore_entry(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "list_versions" => {
            let req = ListVersionsRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.list_versions(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "generate_totp" => {
            let req = GenerateTotpRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.generate_totp(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        // ── Generic secrets dispatch ──────────────────────────────────
        "secret_put" => {
            let req = secrets_proto::PutSecretRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.secrets_svc.put_secret(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "secret_get" => {
            let req = secrets_proto::GetSecretRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.secrets_svc.get_secret(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "secret_delete" => {
            let req = secrets_proto::DeleteSecretRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.secrets_svc.delete_secret(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "secret_restore" => {
            let req = secrets_proto::RestoreSecretRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.secrets_svc.restore_secret(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "secret_list" => {
            let req = secrets_proto::ListSecretsRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.secrets_svc.list_secrets(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "secret_list_versions" => {
            let req = secrets_proto::ListSecretVersionsRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.secrets_svc.list_secret_versions(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "secret_batch_put" => {
            let req = secrets_proto::BatchPutSecretsRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.secrets_svc.batch_put_secrets(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "secret_batch_get" => {
            let req = secrets_proto::BatchGetSecretsRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.secrets_svc.batch_get_secrets(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "secret_delete_version" => {
            let req = secrets_proto::DeleteSecretVersionRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.secrets_svc.delete_secret_version(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "secret_update_description" => {
            let req =
                secrets_proto::UpdateSecretDescriptionRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(
                    plugin
                        .secrets_svc
                        .update_secret_description(Request::new(req)),
                )
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "secret_get_sealed" => {
            let req = secrets_proto::GetSecretRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.secrets_svc.get_secret_sealed(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "secret_put_sealed" => {
            let req = secrets_proto::PutSecretSealedRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.secrets_svc.put_secret_sealed(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        _ => Err(-1), // PluginResult::NotFound
    }
}

/// Translate a Phase P bytes-level gRPC path into a `dispatch_vault`
/// legacy method name, then re-enter. Keeping a single match table
/// per concern avoids duplicating prost-decode + tonic-call boilerplate.
///
/// `full_path` is the gRPC `:path` header verbatim, including the
/// leading `/` (e.g. `/nexus.secrets.v1.GenericSecretsService/PutSecret`).
fn dispatch_grpc(plugin: &VaultPlugin, full_path: &str, payload: &[u8]) -> Result<Vec<u8>, i32> {
    let path = full_path.trim_start_matches('/');
    let (service, method) = path.split_once('/').ok_or(-2)?;
    let legacy = match (service, method) {
        // GenericSecretsService
        ("nexus.secrets.v1.GenericSecretsService", "PutSecret") => "secret_put",
        ("nexus.secrets.v1.GenericSecretsService", "GetSecret") => "secret_get",
        ("nexus.secrets.v1.GenericSecretsService", "DeleteSecret") => "secret_delete",
        ("nexus.secrets.v1.GenericSecretsService", "RestoreSecret") => "secret_restore",
        ("nexus.secrets.v1.GenericSecretsService", "ListSecrets") => "secret_list",
        ("nexus.secrets.v1.GenericSecretsService", "ListSecretVersions") => "secret_list_versions",
        ("nexus.secrets.v1.GenericSecretsService", "BatchPutSecrets") => "secret_batch_put",
        ("nexus.secrets.v1.GenericSecretsService", "BatchGetSecrets") => "secret_batch_get",
        ("nexus.secrets.v1.GenericSecretsService", "DeleteSecretVersion") => {
            "secret_delete_version"
        }
        ("nexus.secrets.v1.GenericSecretsService", "UpdateSecretDescription") => {
            "secret_update_description"
        }
        ("nexus.secrets.v1.GenericSecretsService", "GetSecretSealed") => "secret_get_sealed",
        ("nexus.secrets.v1.GenericSecretsService", "PutSecretSealed") => "secret_put_sealed",
        // PasswordVaultService
        ("nexus.password_vault.v1.PasswordVaultService", "PutEntry") => "put_entry",
        ("nexus.password_vault.v1.PasswordVaultService", "GetEntry") => "get_entry",
        ("nexus.password_vault.v1.PasswordVaultService", "ListEntries") => "list_entries",
        ("nexus.password_vault.v1.PasswordVaultService", "DeleteEntry") => "delete_entry",
        ("nexus.password_vault.v1.PasswordVaultService", "RestoreEntry") => "restore_entry",
        ("nexus.password_vault.v1.PasswordVaultService", "ListVersions") => "list_versions",
        ("nexus.password_vault.v1.PasswordVaultService", "GenerateTotp") => "generate_totp",
        _ => return Err(-1), // PluginResult::NotFound
    };
    dispatch_vault(plugin, legacy, payload)
}

fn status_to_plugin_error(status: tonic::Status) -> i32 {
    match status.code() {
        tonic::Code::NotFound => -1,
        tonic::Code::InvalidArgument => -2,
        _ => -3,
    }
}

declare_service_plugin!("password-vault", VaultPlugin, {
    create: create_vault,
    dispatch: dispatch_vault,
});

// ── Optional Phase P symbol: opt this plugin into cluster gRPC routing ──
//
// Exposes the two gRPC service full names hosted by this plugin so
// `nexusd-cluster` can route external tonic traffic at `/<service>/<method>`
// into our `nexus_service_dispatch`. See `nexus-plugin-abi`'s
// `symbols::SERVICE_GRPC_SERVICES` for the contract. Bytes-level dispatch
// happens via the existing v2 symbol — no API version bump needed.
//
// Storage is `'static` so the kernel never frees the pointer.

/// # Safety
///
/// Pure data lookup — returns a pointer to a `'static` null-terminated
/// JSON byte slice. No invariants required from the caller.
#[no_mangle]
pub unsafe extern "C" fn nexus_plugin_grpc_services() -> *const c_char {
    const SERVICES_JSON: &[u8] = b"[\
        \"nexus.secrets.v1.GenericSecretsService\",\
        \"nexus.password_vault.v1.PasswordVaultService\"\
    ]\0";
    SERVICES_JSON.as_ptr() as *const c_char
}

// ── Dylib E2E tests — load the compiled cdylib via dlopen ─────────
//
// These tests verify the real C ABI boundary: dlopen → symbol lookup →
// dispatch → protobuf → response. They catch ABI mismatches, symbol
// export bugs, and protobuf wire format issues that in-process tests miss.
//
// Prerequisites: `cargo build -p nexus-vault` (debug or release).
// CI runs these after the release build step.

#[cfg(test)]
mod dylib_e2e {
    use std::ffi::{c_char, CStr, CString};
    use std::os::raw::c_void;
    use std::path::PathBuf;
    use std::sync::Mutex;

    use prost::Message;

    use services::generic_secrets::proto as secrets;
    use services::password_vault::proto::*;

    fn encode<M: Message>(msg: &M) -> Vec<u8> {
        let mut buf = Vec::new();
        msg.encode(&mut buf).unwrap();
        buf
    }

    /// Locate the compiled vault cdylib. Prefers `VAULT_DYLIB_PATH` env
    /// var, then falls back to target/{release,debug}/.
    /// Returns `None` when the cdylib hasn't been built (workspace-wide
    /// `cargo test` without prior `cargo build -p nexus-vault`).
    fn find_dylib() -> Option<PathBuf> {
        if let Ok(p) = std::env::var("VAULT_DYLIB_PATH") {
            let path = PathBuf::from(&p);
            assert!(path.exists(), "VAULT_DYLIB_PATH={path:?} does not exist");
            return Some(path);
        }

        let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let target_dir = manifest_dir
            .ancestors()
            .nth(3)
            .expect("workspace root")
            .join("target");

        #[cfg(target_os = "macos")]
        let name = "libnexus_vault.dylib";
        #[cfg(target_os = "linux")]
        let name = "libnexus_vault.so";
        #[cfg(target_os = "windows")]
        let name = "nexus_vault.dll";

        for profile in ["release", "debug"] {
            let p = target_dir.join(profile).join(name);
            if p.exists() {
                return Some(p);
            }
        }

        None
    }

    // Dummy KernelHandle callbacks — create_vault ignores the handle
    // but the C ABI requires a valid struct pointer.
    unsafe extern "C" fn noop_read(
        _: *const c_void,
        _: *const c_char,
        _: *mut *mut u8,
        _: *mut usize,
    ) -> i32 {
        -1
    }
    unsafe extern "C" fn noop_write(
        _: *const c_void,
        _: *const c_char,
        _: *const u8,
        _: usize,
    ) -> i32 {
        -1
    }
    unsafe extern "C" fn noop_stat(
        _: *const c_void,
        _: *const c_char,
        _: *mut *mut u8,
        _: *mut usize,
    ) -> i32 {
        -1
    }
    unsafe extern "C" fn noop_readdir(
        _: *const c_void,
        _: *const c_char,
        _: *mut *mut u8,
        _: *mut usize,
    ) -> i32 {
        -1
    }
    unsafe extern "C" fn noop_unlink(_: *const c_void, _: *const c_char) -> i32 {
        -1
    }
    unsafe extern "C" fn noop_mkdir(_: *const c_void, _: *const c_char) -> i32 {
        -1
    }
    unsafe extern "C" fn noop_rmdir(_: *const c_void, _: *const c_char) -> i32 {
        -1
    }
    unsafe extern "C" fn noop_stat_batch(
        _: *const c_void,
        _: *const c_char,
        _: *mut *mut u8,
        _: *mut usize,
    ) -> i32 {
        -1
    }
    unsafe extern "C" fn noop_rename(_: *const c_void, _: *const c_char, _: *const c_char) -> i32 {
        -1
    }

    fn dummy_kernel_handle() -> nexus_plugin_abi::KernelHandle {
        nexus_plugin_abi::KernelHandle {
            sys_read: noop_read,
            sys_write: noop_write,
            sys_stat: noop_stat,
            sys_readdir: noop_readdir,
            sys_unlink: noop_unlink,
            sys_mkdir: noop_mkdir,
            sys_rmdir: noop_rmdir,
            sys_rename: noop_rename,
            sys_stat_batch: noop_stat_batch,
            free_buf: nexus_plugin_abi::nexus_free,
            kernel_ptr: std::ptr::null(),
        }
    }

    /// Prevents concurrent NEXUS_DATA_DIR mutations across parallel tests.
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    /// Load the vault cdylib and create a service instance backed by a
    /// fresh temp directory. Handles dlopen, env var setup, create, and
    /// destroy (on drop) through the C ABI.
    struct DylibFixture {
        lib: libloading::Library,
        svc: *mut c_void,
        _dir: tempfile::TempDir,
    }

    impl DylibFixture {
        /// Returns `None` when the cdylib hasn't been built — callers
        /// should `return` early to skip the test gracefully.
        fn try_new() -> Option<Self> {
            let dylib_path = find_dylib()?;
            let lib = unsafe { libloading::Library::new(&dylib_path) }
                .unwrap_or_else(|e| panic!("dlopen({dylib_path:?}): {e}"));

            let dir = tempfile::TempDir::new().unwrap();

            let svc = {
                let _lock = ENV_LOCK.lock().unwrap();
                #[allow(unused_unsafe)]
                unsafe {
                    std::env::set_var("NEXUS_DATA_DIR", dir.path());
                }
                let handle = dummy_kernel_handle();
                unsafe {
                    let create: libloading::Symbol<nexus_plugin_abi::ServiceCreateFn> =
                        lib.get(b"nexus_service_create").unwrap();
                    create(&handle)
                }
            };
            assert!(!svc.is_null(), "nexus_service_create returned null");

            Some(Self {
                lib,
                svc,
                _dir: dir,
            })
        }

        /// Dispatch a method through the C ABI, returning the response
        /// bytes or a plugin error code.
        fn dispatch(&self, method: &str, payload: &[u8]) -> Result<Vec<u8>, i32> {
            unsafe {
                let f: libloading::Symbol<nexus_plugin_abi::ServiceDispatchFn> =
                    self.lib.get(b"nexus_service_dispatch").unwrap();
                let method_c = CString::new(method).unwrap();
                let mut out_buf: *mut u8 = std::ptr::null_mut();
                let mut out_len: usize = 0;

                let rc = f(
                    self.svc,
                    method_c.as_ptr(),
                    payload.as_ptr(),
                    payload.len(),
                    &mut out_buf,
                    &mut out_len,
                );

                if rc == 0 {
                    Ok(Vec::from_raw_parts(out_buf, out_len, out_len))
                } else {
                    Err(rc)
                }
            }
        }
    }

    impl Drop for DylibFixture {
        fn drop(&mut self) {
            unsafe {
                let destroy: libloading::Symbol<nexus_plugin_abi::ServiceDestroyFn> =
                    self.lib.get(b"nexus_service_destroy").unwrap();
                destroy(self.svc);
            }
        }
    }

    // ── Test 1: dlopen + verify all C ABI symbols exist ─────────────

    #[test]
    fn dylib_loads_and_exports_symbols() {
        let path = match find_dylib() {
            Some(p) => p,
            None => return, // cdylib not built — skip
        };
        let lib = unsafe { libloading::Library::new(&path) }
            .unwrap_or_else(|e| panic!("dlopen({path:?}): {e}"));

        unsafe {
            let _: libloading::Symbol<unsafe extern "C" fn() -> u32> =
                lib.get(b"nexus_plugin_api_version").expect("api_version");
            let _: libloading::Symbol<unsafe extern "C" fn() -> u32> =
                lib.get(b"nexus_plugin_kind").expect("kind");
            let _: libloading::Symbol<unsafe extern "C" fn() -> *const c_char> =
                lib.get(b"nexus_plugin_name").expect("name");
            let _: libloading::Symbol<nexus_plugin_abi::ServiceCreateFn> =
                lib.get(b"nexus_service_create").expect("create");
            let _: libloading::Symbol<nexus_plugin_abi::ServiceDispatchFn> =
                lib.get(b"nexus_service_dispatch").expect("dispatch");
            let _: libloading::Symbol<nexus_plugin_abi::ServiceDestroyFn> =
                lib.get(b"nexus_service_destroy").expect("destroy");

            // Verify metadata values match compile-time constants.
            let api_ver: libloading::Symbol<unsafe extern "C" fn() -> u32> =
                lib.get(b"nexus_plugin_api_version").unwrap();
            assert_eq!(api_ver(), nexus_plugin_abi::PLUGIN_API_VERSION);

            let kind: libloading::Symbol<unsafe extern "C" fn() -> u32> =
                lib.get(b"nexus_plugin_kind").unwrap();
            assert_eq!(kind(), nexus_plugin_abi::PluginKind::Service as u32);

            let name_fn: libloading::Symbol<unsafe extern "C" fn() -> *const c_char> =
                lib.get(b"nexus_plugin_name").unwrap();
            assert_eq!(
                CStr::from_ptr(name_fn()).to_str().unwrap(),
                "password-vault"
            );

            // ── Phase P opt-in: cluster reads this to route /<svc>/<method>
            //     gRPC traffic into our nexus_service_dispatch. ──
            let grpc_fn: libloading::Symbol<nexus_plugin_abi::PluginGrpcServicesFn> = lib
                .get(nexus_plugin_abi::symbols::SERVICE_GRPC_SERVICES.as_bytes())
                .expect("plugin_grpc_services");
            let json = CStr::from_ptr(grpc_fn()).to_str().unwrap();
            assert_eq!(
                json,
                "[\"nexus.secrets.v1.GenericSecretsService\",\
                 \"nexus.password_vault.v1.PasswordVaultService\"]"
            );
        }
    }

    // ── Test 2: create → dispatch → destroy full lifecycle ──────────

    #[test]
    fn dylib_create_dispatch_destroy_lifecycle() {
        let Some(fix) = DylibFixture::try_new() else {
            return;
        };

        // Put via generic secrets.
        let resp = fix
            .dispatch(
                "secret_put",
                &encode(&secrets::PutSecretRequest {
                    namespace: "test".into(),
                    key: "lifecycle-key".into(),
                    value: "lifecycle-value".into(),
                    description: None,
                }),
            )
            .unwrap();
        let put = secrets::PutSecretResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(put.metadata.unwrap().current_version, 1);

        // Get it back.
        let resp = fix
            .dispatch(
                "secret_get",
                &encode(&secrets::GetSecretRequest {
                    namespace: "test".into(),
                    key: "lifecycle-key".into(),
                    version: None,
                }),
            )
            .unwrap();
        let get = secrets::GetSecretResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(get.value, "lifecycle-value");
        assert_eq!(get.version, 1);

        // Destroy happens in DylibFixture::drop.
    }

    // ── Test 3: generic secrets CRUD via C ABI ──────────────────────

    #[test]
    fn dylib_generic_secrets_crud() {
        let Some(fix) = DylibFixture::try_new() else {
            return;
        };

        // Put
        let resp = fix
            .dispatch(
                "secret_put",
                &encode(&secrets::PutSecretRequest {
                    namespace: "provider:openai".into(),
                    key: "api_key".into(),
                    value: "sk-test-123".into(),
                    description: Some("OpenAI key".into()),
                }),
            )
            .unwrap();
        let meta = secrets::PutSecretResponse::decode(resp.as_slice())
            .unwrap()
            .metadata
            .unwrap();
        assert_eq!(meta.namespace, "provider:openai");
        assert_eq!(meta.key, "api_key");
        assert_eq!(meta.current_version, 1);

        // Get
        let resp = fix
            .dispatch(
                "secret_get",
                &encode(&secrets::GetSecretRequest {
                    namespace: "provider:openai".into(),
                    key: "api_key".into(),
                    version: None,
                }),
            )
            .unwrap();
        let get = secrets::GetSecretResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(get.value, "sk-test-123");

        // List
        let resp = fix
            .dispatch(
                "secret_list",
                &encode(&secrets::ListSecretsRequest {
                    namespace: Some("provider:openai".into()),
                    include_deleted: false,
                }),
            )
            .unwrap();
        let list = secrets::ListSecretsResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(list.count, 1);
        assert_eq!(list.secrets[0].key, "api_key");

        // Delete
        let resp = fix
            .dispatch(
                "secret_delete",
                &encode(&secrets::DeleteSecretRequest {
                    namespace: "provider:openai".into(),
                    key: "api_key".into(),
                }),
            )
            .unwrap();
        let del = secrets::DeleteSecretResponse::decode(resp.as_slice()).unwrap();
        assert!(del.deleted);

        // Get after delete → NotFound
        let err = fix
            .dispatch(
                "secret_get",
                &encode(&secrets::GetSecretRequest {
                    namespace: "provider:openai".into(),
                    key: "api_key".into(),
                    version: None,
                }),
            )
            .unwrap_err();
        assert_eq!(err, -1); // PluginResult::NotFound
    }

    // ── Test 4: cross-service visibility via C ABI ──────────────────

    #[test]
    fn dylib_cross_service_visibility() {
        let Some(fix) = DylibFixture::try_new() else {
            return;
        };

        // Store via PasswordVault dispatch.
        let resp = fix
            .dispatch(
                "put_entry",
                &encode(&PutEntryRequest {
                    entry: Some(VaultEntry {
                        title: "github".into(),
                        username: Some("alice".into()),
                        password: Some("hunter2".into()),
                        url: None,
                        notes: None,
                        tags: None,
                        totp_secret: None,
                        extra_json: None,
                    }),
                    audit: None,
                }),
            )
            .unwrap();
        let put = PutEntryResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(put.title, "github");

        // List via GenericSecrets — password stored under namespace="passwords".
        let resp = fix
            .dispatch(
                "secret_list",
                &encode(&secrets::ListSecretsRequest {
                    namespace: Some("passwords".into()),
                    include_deleted: false,
                }),
            )
            .unwrap();
        let list = secrets::ListSecretsResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(list.count, 1);
        assert_eq!(list.secrets[0].namespace, "passwords");
        assert_eq!(list.secrets[0].key, "github");

        // Get raw value via GenericSecrets.
        let resp = fix
            .dispatch(
                "secret_get",
                &encode(&secrets::GetSecretRequest {
                    namespace: "passwords".into(),
                    key: "github".into(),
                    version: None,
                }),
            )
            .unwrap();
        let get = secrets::GetSecretResponse::decode(resp.as_slice()).unwrap();
        assert!(get.value.contains("hunter2"), "raw JSON contains password");
    }

    // ── Test 5: unknown method returns NOT_FOUND ────────────────────

    #[test]
    fn dylib_invalid_method_returns_error() {
        let Some(fix) = DylibFixture::try_new() else {
            return;
        };
        let err = fix.dispatch("nonexistent_method", &[]).unwrap_err();
        assert_eq!(err, -1); // PluginResult::NotFound
    }
}

// ── Plugin dispatch E2E tests ──────────────────────────────────────

#[cfg(test)]
mod dispatch_e2e {
    use super::*;
    use prost::Message;
    use tempfile::TempDir;

    fn fresh_plugin() -> (TempDir, VaultPlugin) {
        let dir = TempDir::new().unwrap();
        let vault_dir = dir.path().join("vault");
        std::fs::create_dir_all(&vault_dir).unwrap();

        let kernel = Arc::new(kernel::kernel::Kernel::new());
        let meta_path = vault_dir.join("vault-meta.redb");
        if let Some(p) = meta_path.to_str() {
            kernel.set_metastore_path(p).unwrap();
        }

        let content_dir = vault_dir.join("content");
        let backend: Arc<dyn kernel::abc::object_store::ObjectStore> = Arc::new(
            backends::storage::path_local::PathLocalBackend::new(&content_dir, true).unwrap(),
        );
        let backend_name = backend.name().to_string();

        // Mount backend.
        kernel
            .sys_setattr(
                "/vault",
                2,
                &backend_name,
                Some(backend),
                None,
                None,
                "memory",
                "root",
                false,
                0,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )
            .unwrap();

        let master_key_path = vault_dir.join("master.key");
        let master_key =
            services::password_vault::crypto::load_or_create_master_key(&master_key_path).unwrap();

        let secrets_svc =
            GenericSecretsServiceImpl::new_on_existing_mount(kernel, "/vault", master_key).unwrap();
        let svc = PasswordVaultServiceImpl::new_with_secrets(secrets_svc.clone());

        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();

        (
            dir,
            VaultPlugin {
                svc,
                secrets_svc,
                rt,
            },
        )
    }

    fn entry(title: &str, password: &str, totp_secret: Option<&str>) -> VaultEntry {
        VaultEntry {
            title: title.into(),
            username: Some("alice".into()),
            password: Some(password.into()),
            url: Some("https://example.com".into()),
            notes: None,
            tags: None,
            totp_secret: totp_secret.map(String::from),
            extra_json: None,
        }
    }

    fn encode<M: Message>(msg: &M) -> Vec<u8> {
        let mut buf = Vec::new();
        msg.encode(&mut buf).unwrap();
        buf
    }

    // ── Scenario 1: Full credential lifecycle via dispatch ─────────

    #[test]
    fn full_credential_lifecycle_via_dispatch() {
        let (_dir, plugin) = fresh_plugin();

        let put_payload = encode(&PutEntryRequest {
            entry: Some(entry("github", "hunter2", None)),
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "put_entry", &put_payload).unwrap();
        let put_resp = PutEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(put_resp.title, "github");
        assert_eq!(put_resp.version, 1);

        let get_payload = encode(&GetEntryRequest {
            title: put_resp.title.clone(),
            version: None,
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "get_entry", &get_payload).unwrap();
        let get_resp = GetEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(get_resp.entry.as_ref().unwrap().title, "github");
        assert_eq!(
            get_resp.entry.as_ref().unwrap().password.as_deref(),
            Some("hunter2")
        );

        let list_payload = encode(&ListEntriesRequest {
            query: String::new(),
            limit: 0,
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "list_entries", &list_payload).unwrap();
        let list_resp = ListEntriesResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(list_resp.total_in_vault, 1);
        assert_eq!(list_resp.entries[0].title, "github");

        let del_payload = encode(&DeleteEntryRequest {
            title: put_resp.title.clone(),
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "delete_entry", &del_payload).unwrap();
        let del_resp = DeleteEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        assert!(del_resp.deleted);

        let err = dispatch_vault(&plugin, "get_entry", &get_payload).unwrap_err();
        assert_eq!(err, -1);

        let restore_payload = encode(&RestoreEntryRequest {
            title: put_resp.title.clone(),
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "restore_entry", &restore_payload).unwrap();
        let restore_resp = RestoreEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        assert!(restore_resp.restored);

        let resp_bytes = dispatch_vault(&plugin, "get_entry", &get_payload).unwrap();
        let get_resp = GetEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(get_resp.entry.unwrap().password.as_deref(), Some("hunter2"));
    }

    // ── Scenario 2: Password rotation with version history ─────────

    #[test]
    fn password_rotation_with_version_history() {
        let (_dir, plugin) = fresh_plugin();

        for (i, pw) in ["initial-pw", "rotated-pw", "final-pw"].iter().enumerate() {
            let payload = encode(&PutEntryRequest {
                entry: Some(entry("aws-prod", pw, None)),
                audit: None,
            });
            let resp_bytes = dispatch_vault(&plugin, "put_entry", &payload).unwrap();
            let resp = PutEntryResponse::decode(resp_bytes.as_slice()).unwrap();
            assert_eq!(resp.version, (i + 1) as i32);
        }

        let lv_payload = encode(&ListVersionsRequest {
            title: "aws-prod".into(),
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "list_versions", &lv_payload).unwrap();
        let lv_resp = ListVersionsResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(lv_resp.count, 3);
        let vers: Vec<i32> = lv_resp.versions.iter().map(|v| v.version).collect();
        assert_eq!(vers, vec![1, 2, 3]);

        let get_v1 = encode(&GetEntryRequest {
            title: "aws-prod".into(),
            version: Some(1),
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "get_entry", &get_v1).unwrap();
        let resp = GetEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(resp.entry.unwrap().password.as_deref(), Some("initial-pw"));

        let get_latest = encode(&GetEntryRequest {
            title: "aws-prod".into(),
            version: None,
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "get_entry", &get_latest).unwrap();
        let resp = GetEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(resp.entry.unwrap().password.as_deref(), Some("final-pw"));
    }

    // ── Scenario 3: TOTP survives password rotation ────────────────

    #[test]
    fn totp_survives_password_rotation() {
        let (_dir, plugin) = fresh_plugin();

        let payload = encode(&PutEntryRequest {
            entry: Some(entry("aws", "original-pw", Some("JBSWY3DPEHPK3PXP"))),
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "put_entry", &payload).unwrap();
        let put_resp = PutEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(put_resp.version, 1);

        let totp_payload = encode(&GenerateTotpRequest {
            title: "aws".into(),
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "generate_totp", &totp_payload).unwrap();
        let totp1 = GenerateTotpResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(totp1.code.len(), 6);
        assert!(totp1.code.chars().all(|c| c.is_ascii_digit()));
        assert_eq!(totp1.period_seconds, 30);

        let payload = encode(&PutEntryRequest {
            entry: Some(entry("aws", "rotated-pw", Some("JBSWY3DPEHPK3PXP"))),
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "put_entry", &payload).unwrap();
        let put_resp = PutEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(put_resp.version, 2);

        let resp_bytes = dispatch_vault(&plugin, "generate_totp", &totp_payload).unwrap();
        let totp2 = GenerateTotpResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(totp1.code, totp2.code);

        let get_payload = encode(&GetEntryRequest {
            title: "aws".into(),
            version: None,
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "get_entry", &get_payload).unwrap();
        let get_resp = GetEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        let e = get_resp.entry.unwrap();
        assert_eq!(e.password.as_deref(), Some("rotated-pw"));
        assert!(e.totp_secret.is_none());
    }

    // ── Scenario 4: Multi-credential search and cleanup ────────────

    #[test]
    fn multi_credential_search_and_cleanup() {
        let (_dir, plugin) = fresh_plugin();

        for (title, pw) in [("gmail", "pw1"), ("github", "pw2"), ("aws-prod", "pw3")] {
            let payload = encode(&PutEntryRequest {
                entry: Some(entry(title, pw, None)),
                audit: None,
            });
            dispatch_vault(&plugin, "put_entry", &payload).unwrap();
        }

        let list_payload = encode(&ListEntriesRequest {
            query: "git".into(),
            limit: 0,
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "list_entries", &list_payload).unwrap();
        let list_resp = ListEntriesResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(list_resp.matched, 1);
        assert_eq!(list_resp.total_in_vault, 3);
        assert_eq!(list_resp.entries[0].title, "github");

        let del_payload = encode(&DeleteEntryRequest {
            title: "github".into(),
            audit: None,
        });
        dispatch_vault(&plugin, "delete_entry", &del_payload).unwrap();

        let list_all = encode(&ListEntriesRequest {
            query: String::new(),
            limit: 0,
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "list_entries", &list_all).unwrap();
        let list_resp = ListEntriesResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(list_resp.total_in_vault, 2);
        let titles: Vec<&str> = list_resp.entries.iter().map(|e| e.title.as_str()).collect();
        assert!(!titles.contains(&"github"));
    }

    // ── Scenario 5: Unknown method returns NotFound ────────────────

    #[test]
    fn unknown_method_returns_not_found() {
        let (_dir, plugin) = fresh_plugin();
        let err = dispatch_vault(&plugin, "nonexistent_method", &[]).unwrap_err();
        assert_eq!(err, -1);
    }

    // ── Scenario 6: Invalid protobuf returns InvalidArgument ───────

    #[test]
    fn invalid_protobuf_returns_invalid_argument() {
        let (_dir, plugin) = fresh_plugin();
        let err = dispatch_vault(&plugin, "get_entry", b"not-valid-protobuf").unwrap_err();
        assert_eq!(err, -2);
    }

    // ── Generic secrets dispatch tests ────────────────────────────

    #[test]
    fn generic_secret_full_lifecycle_via_dispatch() {
        let (_dir, plugin) = fresh_plugin();

        let put_payload = encode(&secrets_proto::PutSecretRequest {
            namespace: "provider:openai".into(),
            key: "api_key".into(),
            value: "sk-test-123".into(),
            description: Some("OpenAI prod key".into()),
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_put", &put_payload).unwrap();
        let put_resp = secrets_proto::PutSecretResponse::decode(resp_bytes.as_slice()).unwrap();
        let meta = put_resp.metadata.unwrap();
        assert_eq!(meta.namespace, "provider:openai");
        assert_eq!(meta.key, "api_key");
        assert_eq!(meta.current_version, 1);
        assert!(!meta.deleted);

        let get_payload = encode(&secrets_proto::GetSecretRequest {
            namespace: "provider:openai".into(),
            key: "api_key".into(),
            version: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_get", &get_payload).unwrap();
        let get_resp = secrets_proto::GetSecretResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(get_resp.value, "sk-test-123");
        assert_eq!(get_resp.version, 1);

        let list_payload = encode(&secrets_proto::ListSecretsRequest {
            namespace: Some("provider:openai".into()),
            include_deleted: false,
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_list", &list_payload).unwrap();
        let list_resp = secrets_proto::ListSecretsResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(list_resp.count, 1);
        assert_eq!(list_resp.secrets[0].key, "api_key");

        let del_payload = encode(&secrets_proto::DeleteSecretRequest {
            namespace: "provider:openai".into(),
            key: "api_key".into(),
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_delete", &del_payload).unwrap();
        let del_resp = secrets_proto::DeleteSecretResponse::decode(resp_bytes.as_slice()).unwrap();
        assert!(del_resp.deleted);

        let err = dispatch_vault(&plugin, "secret_get", &get_payload).unwrap_err();
        assert_eq!(err, -1);

        let restore_payload = encode(&secrets_proto::RestoreSecretRequest {
            namespace: "provider:openai".into(),
            key: "api_key".into(),
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_restore", &restore_payload).unwrap();
        let restore_resp =
            secrets_proto::RestoreSecretResponse::decode(resp_bytes.as_slice()).unwrap();
        assert!(restore_resp.restored);

        let resp_bytes = dispatch_vault(&plugin, "secret_get", &get_payload).unwrap();
        let get_resp = secrets_proto::GetSecretResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(get_resp.value, "sk-test-123");
    }

    #[test]
    fn generic_secret_namespace_isolation() {
        let (_dir, plugin) = fresh_plugin();

        for (ns, k, v) in [
            ("channel:telegram:1", "token", "tok-123"),
            ("channel:lark:2", "appSecret", "lark-secret"),
            ("provider:openai", "api_key", "sk-456"),
        ] {
            let payload = encode(&secrets_proto::PutSecretRequest {
                namespace: ns.into(),
                key: k.into(),
                value: v.into(),
                description: None,
            });
            dispatch_vault(&plugin, "secret_put", &payload).unwrap();
        }

        let list_payload = encode(&secrets_proto::ListSecretsRequest {
            namespace: Some("channel:telegram:1".into()),
            include_deleted: false,
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_list", &list_payload).unwrap();
        let list_resp = secrets_proto::ListSecretsResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(list_resp.count, 1);
        assert_eq!(list_resp.secrets[0].namespace, "channel:telegram:1");

        let list_all = encode(&secrets_proto::ListSecretsRequest {
            namespace: None,
            include_deleted: false,
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_list", &list_all).unwrap();
        let list_resp = secrets_proto::ListSecretsResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(list_resp.count, 3);
    }

    #[test]
    fn generic_secret_batch_operations_via_dispatch() {
        let (_dir, plugin) = fresh_plugin();

        let batch_put_payload = encode(&secrets_proto::BatchPutSecretsRequest {
            secrets: vec![
                secrets_proto::PutSecretRequest {
                    namespace: "auth:jwt".into(),
                    key: "webui_secret".into(),
                    value: "jwt-secret-value".into(),
                    description: Some("JWT signing key".into()),
                },
                secrets_proto::PutSecretRequest {
                    namespace: "auth:acp:github".into(),
                    key: "auth_token".into(),
                    value: "ghp_token123".into(),
                    description: None,
                },
            ],
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_batch_put", &batch_put_payload).unwrap();
        let batch_put_resp =
            secrets_proto::BatchPutSecretsResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(batch_put_resp.count, 2);

        let batch_get_payload = encode(&secrets_proto::BatchGetSecretsRequest {
            queries: vec![
                secrets_proto::GetSecretRequest {
                    namespace: "auth:jwt".into(),
                    key: "webui_secret".into(),
                    version: None,
                },
                secrets_proto::GetSecretRequest {
                    namespace: "auth:acp:github".into(),
                    key: "auth_token".into(),
                    version: None,
                },
                secrets_proto::GetSecretRequest {
                    namespace: "nonexistent".into(),
                    key: "nope".into(),
                    version: None,
                },
            ],
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_batch_get", &batch_get_payload).unwrap();
        let batch_get_resp =
            secrets_proto::BatchGetSecretsResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(batch_get_resp.count, 2);
        assert_eq!(
            batch_get_resp.secrets["auth:jwt:webui_secret"],
            "jwt-secret-value"
        );
        assert_eq!(
            batch_get_resp.secrets["auth:acp:github:auth_token"],
            "ghp_token123"
        );
    }

    #[test]
    fn generic_secret_version_history_via_dispatch() {
        let (_dir, plugin) = fresh_plugin();

        for (i, val) in ["v1", "v2", "v3"].iter().enumerate() {
            let payload = encode(&secrets_proto::PutSecretRequest {
                namespace: "provider:openai".into(),
                key: "api_key".into(),
                value: val.to_string(),
                description: None,
            });
            let resp_bytes = dispatch_vault(&plugin, "secret_put", &payload).unwrap();
            let resp = secrets_proto::PutSecretResponse::decode(resp_bytes.as_slice()).unwrap();
            assert_eq!(resp.metadata.unwrap().current_version, (i + 1) as i32);
        }

        let lv_payload = encode(&secrets_proto::ListSecretVersionsRequest {
            namespace: "provider:openai".into(),
            key: "api_key".into(),
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_list_versions", &lv_payload).unwrap();
        let lv_resp =
            secrets_proto::ListSecretVersionsResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(lv_resp.count, 3);
        let vers: Vec<i32> = lv_resp.versions.iter().map(|v| v.version).collect();
        assert_eq!(vers, vec![1, 2, 3]);

        let get_v1 = encode(&secrets_proto::GetSecretRequest {
            namespace: "provider:openai".into(),
            key: "api_key".into(),
            version: Some(1),
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_get", &get_v1).unwrap();
        let resp = secrets_proto::GetSecretResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(resp.value, "v1");

        let get_latest = encode(&secrets_proto::GetSecretRequest {
            namespace: "provider:openai".into(),
            key: "api_key".into(),
            version: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_get", &get_latest).unwrap();
        let resp = secrets_proto::GetSecretResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(resp.value, "v3");
    }

    // ── Scenario 11: Cross-service visibility ──────────────────────
    //
    // Password entries stored via PasswordVaultService should be visible
    // via GenericSecretsService in namespace="passwords", and vice versa.
    // Data flow: put_entry returns title → secret_list finds it →
    //   secret_get uses returned key → verify JSON contains password.

    #[test]
    fn password_entries_visible_via_generic_secrets_api() {
        let (_dir, plugin) = fresh_plugin();

        // Step 1: Store via password vault — capture returned title
        let put_payload = encode(&PutEntryRequest {
            entry: Some(entry("github", "hunter2", None)),
            audit: None,
        });
        let resp = dispatch_vault(&plugin, "put_entry", &put_payload).unwrap();
        let put_resp = PutEntryResponse::decode(resp.as_slice()).unwrap();
        let title = put_resp.title; // ← data flows from here

        // Step 2: List via generic secrets — use namespace to find the entry
        let list_payload = encode(&secrets_proto::ListSecretsRequest {
            namespace: Some("passwords".into()),
            include_deleted: false,
        });
        let resp = dispatch_vault(&plugin, "secret_list", &list_payload).unwrap();
        let list_resp = secrets_proto::ListSecretsResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(list_resp.count, 1);
        assert_eq!(list_resp.secrets[0].namespace, "passwords");
        let discovered_key = &list_resp.secrets[0].key; // ← data flows
        assert_eq!(discovered_key, &title, "discovered key matches put title");

        // Step 3: Get raw value using key from step 2
        let get_payload = encode(&secrets_proto::GetSecretRequest {
            namespace: "passwords".into(),
            key: discovered_key.clone(),
            version: None,
        });
        let resp = dispatch_vault(&plugin, "secret_get", &get_payload).unwrap();
        let get_resp = secrets_proto::GetSecretResponse::decode(resp.as_slice()).unwrap();
        assert!(
            get_resp.value.contains("hunter2"),
            "raw JSON contains password"
        );
        assert!(
            get_resp.value.contains(&format!("\"title\":\"{}\"", title)),
            "raw JSON title matches put title"
        );
    }

    // ── Scenario 12: Coexistence — password + generic secrets ──────
    //
    // Both services share /vault/entries/ and /vault/versions/.
    // User problem: "Store passwords AND API keys, then selectively
    //   delete one type without affecting the other."
    // Data flow: put returns titles/keys → list uses namespace to scope
    //   → delete uses returned key → verify cross-type isolation.

    #[test]
    fn coexistence_password_and_generic_secrets_no_interference() {
        let (_dir, plugin) = fresh_plugin();

        // Step 1: Store passwords — capture returned titles
        let mut pw_titles = Vec::new();
        for (title, pw) in [("gmail", "pw1"), ("github", "pw2")] {
            let payload = encode(&PutEntryRequest {
                entry: Some(entry(title, pw, None)),
                audit: None,
            });
            let resp = dispatch_vault(&plugin, "put_entry", &payload).unwrap();
            let put_resp = PutEntryResponse::decode(resp.as_slice()).unwrap();
            pw_titles.push(put_resp.title);
        }
        assert_eq!(pw_titles.len(), 2);

        // Step 2: Store generic secrets — capture returned metadata
        let mut secret_keys: Vec<(String, String)> = Vec::new();
        for (ns, k, v) in [
            ("provider:openai", "api_key", "sk-123"),
            ("auth:jwt", "webui_secret", "jwt-val"),
        ] {
            let payload = encode(&secrets_proto::PutSecretRequest {
                namespace: ns.into(),
                key: k.into(),
                value: v.into(),
                description: None,
            });
            let resp = dispatch_vault(&plugin, "secret_put", &payload).unwrap();
            let meta = secrets_proto::PutSecretResponse::decode(resp.as_slice())
                .unwrap()
                .metadata
                .unwrap();
            secret_keys.push((meta.namespace, meta.key));
        }
        assert_eq!(secret_keys.len(), 2);

        // Step 3: Password list sees only passwords (no generic secrets leaking)
        let list_pw = encode(&ListEntriesRequest {
            query: String::new(),
            limit: 0,
            audit: None,
        });
        let resp = dispatch_vault(&plugin, "list_entries", &list_pw).unwrap();
        let list_resp = ListEntriesResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(list_resp.total_in_vault, 2);
        let listed_titles: Vec<&str> = list_resp.entries.iter().map(|e| e.title.as_str()).collect();
        for t in &pw_titles {
            assert!(
                listed_titles.contains(&t.as_str()),
                "password title {t} visible"
            );
        }

        // Step 4: Generic list (all namespaces): 4 total
        let list_all = encode(&secrets_proto::ListSecretsRequest {
            namespace: None,
            include_deleted: false,
        });
        let resp = dispatch_vault(&plugin, "secret_list", &list_all).unwrap();
        let list_resp = secrets_proto::ListSecretsResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(list_resp.count, 4, "2 passwords + 2 generic = 4 total");

        // Step 5: Delete a generic secret using key from step 2
        let (del_ns, del_key) = &secret_keys[0]; // provider:openai / api_key
        let del = encode(&secrets_proto::DeleteSecretRequest {
            namespace: del_ns.clone(),
            key: del_key.clone(),
        });
        dispatch_vault(&plugin, "secret_delete", &del).unwrap();

        // Step 6: Password list still has 2 (cross-type isolation)
        let resp = dispatch_vault(&plugin, "list_entries", &list_pw).unwrap();
        let list_resp = ListEntriesResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(
            list_resp.total_in_vault, 2,
            "password entries unaffected by generic delete"
        );

        // Step 7: Delete a password using title from step 1
        let del_pw = encode(&DeleteEntryRequest {
            title: pw_titles[0].clone(),
            audit: None,
        });
        dispatch_vault(&plugin, "delete_entry", &del_pw).unwrap();

        // Step 8: Surviving generic secret value intact
        let (surv_ns, surv_key) = &secret_keys[1]; // auth:jwt / webui_secret
        let get = encode(&secrets_proto::GetSecretRequest {
            namespace: surv_ns.clone(),
            key: surv_key.clone(),
            version: None,
        });
        let resp = dispatch_vault(&plugin, "secret_get", &get).unwrap();
        let get_resp = secrets_proto::GetSecretResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(
            get_resp.value, "jwt-val",
            "generic secret value preserved after password delete"
        );
    }

    // ── Scenario 13: Kernel readdir verifies unified VFS layout ────
    //
    // User problem: "After storage merge, verify the kernel VFS shows
    //   the correct unified directory structure."
    // Data flow: put returns title/key → readdir uses those to find
    //   exact paths → version paths derived from put metadata.
    //
    // Uses fresh_plugin_with_kernel() for direct kernel access.

    fn fresh_plugin_with_kernel() -> (TempDir, VaultPlugin, Arc<kernel::kernel::Kernel>) {
        let dir = TempDir::new().unwrap();
        let vault_dir = dir.path().join("vault");
        std::fs::create_dir_all(&vault_dir).unwrap();

        let kernel = Arc::new(kernel::kernel::Kernel::new());
        let meta_path = vault_dir.join("vault-meta.redb");
        if let Some(p) = meta_path.to_str() {
            kernel.set_metastore_path(p).unwrap();
        }
        let content_dir = vault_dir.join("content");
        let backend: Arc<dyn kernel::abc::object_store::ObjectStore> = Arc::new(
            backends::storage::path_local::PathLocalBackend::new(&content_dir, true).unwrap(),
        );
        let backend_name = backend.name().to_string();
        kernel
            .sys_setattr(
                "/vault",
                2,
                &backend_name,
                Some(backend),
                None,
                None,
                "memory",
                "root",
                false,
                0,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )
            .unwrap();
        let master_key = services::password_vault::crypto::load_or_create_master_key(
            &vault_dir.join("master.key"),
        )
        .unwrap();
        let secrets_svc =
            GenericSecretsServiceImpl::new_on_existing_mount(kernel.clone(), "/vault", master_key)
                .unwrap();
        let svc = PasswordVaultServiceImpl::new_with_secrets(secrets_svc.clone());
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();

        (
            dir,
            VaultPlugin {
                svc,
                secrets_svc,
                rt,
            },
            kernel,
        )
    }

    #[test]
    fn unified_vfs_layout_verified_via_kernel_readdir() {
        let (_dir, plugin, kernel) = fresh_plugin_with_kernel();

        // Step 1: Store password — capture returned title
        let payload = encode(&PutEntryRequest {
            entry: Some(entry("github", "pw1", None)),
            audit: None,
        });
        let resp = dispatch_vault(&plugin, "put_entry", &payload).unwrap();
        let pw_title = PutEntryResponse::decode(resp.as_slice()).unwrap().title;

        // Step 2: Store generic secret. Uses `provider:openai` (with a
        // colon — an NTFS-illegal char) on purpose, to make this test
        // also assert the post-encoding contract: namespace/key segments
        // on disk are percent-encoded, while the wire-level API echoes
        // back the original literal.
        let payload = encode(&secrets_proto::PutSecretRequest {
            namespace: "provider:openai".into(),
            key: "api_key".into(),
            value: "sk-456".into(),
            description: None,
        });
        let resp = dispatch_vault(&plugin, "secret_put", &payload).unwrap();
        let meta = secrets_proto::PutSecretResponse::decode(resp.as_slice())
            .unwrap()
            .metadata
            .unwrap();
        // API contract: returned namespace/key are the original strings,
        // NOT the on-disk encoded form.
        assert_eq!(meta.namespace, "provider:openai");
        assert_eq!(meta.key, "api_key");
        // logical (API) value — returned verbatim by the wire layer
        let secret_ns_api = meta.namespace;
        // Physical layout contract: disk segments are percent-encoded.
        // `:` is encoded as `%3A`; `api_key` is safe → identity.
        let secret_ns_disk = "provider%3Aopenai";
        let secret_key_disk = "api_key";

        // Step 3: Verify /vault/entries/ shows the ENCODED namespace dir
        // (not the API literal). This is what readdir actually returns.
        let entries_root = kernel.sys_readdir(
            "/vault/entries",
            "root",
            true,
            kernel::kernel::syscall::ReaddirOpts::default(),
        );
        let ns_names: Vec<&str> = entries_root
            .iter()
            .filter_map(|(p, _)| p.rsplit('/').next())
            .collect();
        assert!(
            ns_names.contains(&"passwords"),
            "passwords namespace dir exists (no encoding — ASCII-safe)"
        );
        assert!(
            ns_names.contains(&secret_ns_disk),
            "secret namespace dir exists IN ENCODED FORM ({secret_ns_disk}), not as API literal {secret_ns_api}"
        );
        // Explicitly assert the API literal is NOT what shows up on disk.
        assert!(
            !ns_names.contains(&secret_ns_api.as_str()),
            "raw colon must not survive on disk — it would break NTFS"
        );

        // Step 4: Verify password entry path uses title from step 1
        let pw_entries = kernel.sys_readdir(
            "/vault/entries/passwords",
            "root",
            true,
            kernel::kernel::syscall::ReaddirOpts::default(),
        );
        assert_eq!(pw_entries.len(), 1);
        assert!(
            pw_entries[0].0.ends_with(&format!("/{pw_title}")),
            "password entry filename matches put title"
        );

        // Step 5: Verify generic secret entry path. Direct readdir into
        // the storage layout MUST use the encoded namespace segment;
        // walking via the API literal would simply not exist on disk.
        let secret_entries = kernel.sys_readdir(
            &format!("/vault/entries/{secret_ns_disk}"),
            "root",
            true,
            kernel::kernel::syscall::ReaddirOpts::default(),
        );
        assert_eq!(secret_entries.len(), 1);
        assert!(
            secret_entries[0]
                .0
                .ends_with(&format!("/{secret_key_disk}")),
            "secret entry filename matches put key (encoded)"
        );

        // Step 6: Verify version files exist under unified versions/ tree
        let pw_vers = kernel.sys_readdir(
            &format!("/vault/versions/passwords/{pw_title}"),
            "root",
            true,
            kernel::kernel::syscall::ReaddirOpts::default(),
        );
        assert_eq!(pw_vers.len(), 1, "1 version for password");

        let secret_vers = kernel.sys_readdir(
            &format!("/vault/versions/{secret_ns_disk}/{secret_key_disk}"),
            "root",
            true,
            kernel::kernel::syscall::ReaddirOpts::default(),
        );
        assert_eq!(secret_vers.len(), 1, "1 version for generic secret");
    }

    // ── Scenario 14: Bidirectional mutation via both APIs ───────────
    //
    // Write a password via PasswordVaultService, update it via
    // GenericSecretsService (raw JSON), read back via PasswordVaultService.

    #[test]
    fn bidirectional_mutation_password_then_generic_then_password() {
        let (_dir, plugin) = fresh_plugin();

        // Step 1: Create password entry via PasswordVaultService
        let payload = encode(&PutEntryRequest {
            entry: Some(entry("aws", "original-pw", None)),
            audit: None,
        });
        let resp = dispatch_vault(&plugin, "put_entry", &payload).unwrap();
        let put_resp = PutEntryResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(put_resp.version, 1);

        // Step 2: Read the raw JSON via GenericSecretsService
        let get_raw = encode(&secrets_proto::GetSecretRequest {
            namespace: "passwords".into(),
            key: "aws".into(),
            version: None,
        });
        let resp = dispatch_vault(&plugin, "secret_get", &get_raw).unwrap();
        let raw = secrets_proto::GetSecretResponse::decode(resp.as_slice()).unwrap();
        assert!(raw.value.contains("\"password\":\"original-pw\""));

        // Step 3: Overwrite via GenericSecretsService with modified JSON
        let modified_json = raw.value.replace("original-pw", "modified-via-generic");
        let put_raw = encode(&secrets_proto::PutSecretRequest {
            namespace: "passwords".into(),
            key: "aws".into(),
            value: modified_json,
            description: None,
        });
        let resp = dispatch_vault(&plugin, "secret_put", &put_raw).unwrap();
        let meta = secrets_proto::PutSecretResponse::decode(resp.as_slice())
            .unwrap()
            .metadata
            .unwrap();
        assert_eq!(meta.current_version, 2);

        // Step 4: Read back via PasswordVaultService — should see the change
        let get_pw = encode(&GetEntryRequest {
            title: "aws".into(),
            version: None,
            audit: None,
        });
        let resp = dispatch_vault(&plugin, "get_entry", &get_pw).unwrap();
        let get_resp = GetEntryResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(
            get_resp.entry.unwrap().password.as_deref(),
            Some("modified-via-generic"),
            "mutation via GenericSecretsService visible through PasswordVaultService"
        );
        assert_eq!(get_resp.version, 2);

        // Step 5: Version history shows both versions
        let lv = encode(&ListVersionsRequest {
            title: "aws".into(),
            audit: None,
        });
        let resp = dispatch_vault(&plugin, "list_versions", &lv).unwrap();
        let lv_resp = ListVersionsResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(lv_resp.count, 2);
    }

    // ── Scenario 15: Real-disk PathLocalBackend accepts `%` in path
    //                segments (the namespace percent-encoding "head
    //                constraint" turned into executable evidence).
    //
    // Background: `zazzy-chasing-pizza.md` assumes the kernel + backend
    // write path does not reject `%` characters. Source of that claim is
    // the external `nexus-vfs` crate (pinned by commit in workspace
    // Cargo.toml) — its source is not vendored, so this test exists to
    // make the assumption fail loudly under CI three-platform matrix if
    // a future bump rejects `%`.
    //
    // Crucially this test uses `fresh_plugin` (PathLocalBackend on a
    // TempDir), not `mount_and_create` from storage.rs which is
    // MemBackend-backed and bypasses real filesystem rules.

    #[test]
    fn pathlocal_backend_accepts_percent_in_namespace() {
        let (_dir, plugin) = fresh_plugin();

        let put_payload = encode(&secrets_proto::PutSecretRequest {
            namespace: "ns%test".into(),
            key: "k".into(),
            value: "v".into(),
            description: None,
        });
        // The whole point: this must not error on Windows / Linux / macOS.
        dispatch_vault(&plugin, "secret_put", &put_payload).unwrap();

        let get_payload = encode(&secrets_proto::GetSecretRequest {
            namespace: "ns%test".into(),
            key: "k".into(),
            version: None,
        });
        let resp = dispatch_vault(&plugin, "secret_get", &get_payload).unwrap();
        let got = secrets_proto::GetSecretResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(got.value, "v");
        assert_eq!(got.namespace, "ns%test", "namespace returned unencoded");
    }

    // ── Scenario 16: Verification step 2 — colon namespace round-trip on
    //                real disk, value + namespace echo identity.
    //
    // Maps to zazzy verification step 2: secret_put with namespace
    // "service:shareone" key "X-API-Key" must round-trip on PathLocalBackend
    // and the list must echo namespace unencoded.

    #[test]
    fn colon_namespace_round_trip_on_real_disk() {
        let (_dir, plugin) = fresh_plugin();

        let put_payload = encode(&secrets_proto::PutSecretRequest {
            namespace: "service:shareone".into(),
            key: "X-API-Key".into(),
            value: "sk-prod-123".into(),
            description: Some("AI Platform key".into()),
        });
        dispatch_vault(&plugin, "secret_put", &put_payload).unwrap();

        let get_payload = encode(&secrets_proto::GetSecretRequest {
            namespace: "service:shareone".into(),
            key: "X-API-Key".into(),
            version: None,
        });
        let got_resp = dispatch_vault(&plugin, "secret_get", &get_payload).unwrap();
        let got = secrets_proto::GetSecretResponse::decode(got_resp.as_slice()).unwrap();
        assert_eq!(got.value, "sk-prod-123");

        let list_payload = encode(&secrets_proto::ListSecretsRequest {
            namespace: Some("service:shareone".into()),
            include_deleted: false,
        });
        let list_resp = dispatch_vault(&plugin, "secret_list", &list_payload).unwrap();
        let list = secrets_proto::ListSecretsResponse::decode(list_resp.as_slice()).unwrap();
        assert_eq!(list.count, 1);
        assert_eq!(
            list.secrets[0].namespace, "service:shareone",
            "namespace must echo original, NOT percent-encoded"
        );
        assert!(
            !list.secrets[0].namespace.contains('%'),
            "no encoded form must leak through API"
        );
    }

    // ── Scenario 17: Verification step 3 — slash in namespace does NOT
    //                split into directory levels on real disk.

    #[test]
    fn slash_namespace_does_not_split_directories_on_real_disk() {
        let (_dir, plugin) = fresh_plugin();

        let put_payload = encode(&secrets_proto::PutSecretRequest {
            namespace: "a/b/c".into(),
            key: "k".into(),
            value: "v".into(),
            description: None,
        });
        dispatch_vault(&plugin, "secret_put", &put_payload).unwrap();

        let list_payload = encode(&secrets_proto::ListSecretsRequest {
            namespace: None,
            include_deleted: false,
        });
        let list_resp = dispatch_vault(&plugin, "secret_list", &list_payload).unwrap();
        let list = secrets_proto::ListSecretsResponse::decode(list_resp.as_slice()).unwrap();
        // Exactly one secret, with the original namespace "a/b/c" intact.
        assert_eq!(list.count, 1);
        assert_eq!(list.secrets[0].namespace, "a/b/c");
        assert_eq!(list.secrets[0].key, "k");
    }

    // ── Scenario 18: Verification step 4 — multi-character combination
    //                (`:` and `/`) on real disk.

    #[test]
    fn mixed_special_chars_namespace_round_trip_on_real_disk() {
        let (_dir, plugin) = fresh_plugin();

        let ns = "service:shareone/v2";
        let put_payload = encode(&secrets_proto::PutSecretRequest {
            namespace: ns.into(),
            key: "k".into(),
            value: "v".into(),
            description: None,
        });
        dispatch_vault(&plugin, "secret_put", &put_payload).unwrap();

        let get_payload = encode(&secrets_proto::GetSecretRequest {
            namespace: ns.into(),
            key: "k".into(),
            version: None,
        });
        let resp = dispatch_vault(&plugin, "secret_get", &get_payload).unwrap();
        let got = secrets_proto::GetSecretResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(got.value, "v");

        let list_payload = encode(&secrets_proto::ListSecretsRequest {
            namespace: Some(ns.into()),
            include_deleted: false,
        });
        let list_resp = dispatch_vault(&plugin, "secret_list", &list_payload).unwrap();
        let list = secrets_proto::ListSecretsResponse::decode(list_resp.as_slice()).unwrap();
        assert_eq!(list.count, 1);
        assert_eq!(list.secrets[0].namespace, ns);
    }

    // ── Scenario 19: Verification step 5 — full lifecycle of a secret
    //                whose namespace contains `:`, exercising all 6
    //                outward operations (put / get / list / delete /
    //                restore / list_versions) plus delete_secret_version.
    //
    // This is the documented regression net for ns_version_dir-class
    // omissions: if any path helper failed to escape, one of these steps
    // would fail on Windows even when the simpler put/get test passed.

    #[test]
    fn full_lifecycle_with_colon_namespace_on_real_disk() {
        let (_dir, plugin) = fresh_plugin();
        let ns = "service:shareone";
        let key = "X-API-Key";

        // Step 1: put twice — version 1, then version 2 (rotation).
        for val in ["v1", "v2"] {
            let payload = encode(&secrets_proto::PutSecretRequest {
                namespace: ns.into(),
                key: key.into(),
                value: val.to_string(),
                description: None,
            });
            dispatch_vault(&plugin, "secret_put", &payload).unwrap();
        }

        // Step 2: get latest — must be v2.
        let get_payload = encode(&secrets_proto::GetSecretRequest {
            namespace: ns.into(),
            key: key.into(),
            version: None,
        });
        let resp = dispatch_vault(&plugin, "secret_get", &get_payload).unwrap();
        let got = secrets_proto::GetSecretResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(got.value, "v2");

        // Step 3: list_versions — must see both v1 and v2.
        let lv_payload = encode(&secrets_proto::ListSecretVersionsRequest {
            namespace: ns.into(),
            key: key.into(),
        });
        let resp = dispatch_vault(&plugin, "secret_list_versions", &lv_payload).unwrap();
        let lv = secrets_proto::ListSecretVersionsResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(lv.count, 2);
        let vers: Vec<i32> = lv.versions.iter().map(|v| v.version).collect();
        assert_eq!(vers, vec![1, 2]);

        // Step 4: delete (tombstone).
        let del_payload = encode(&secrets_proto::DeleteSecretRequest {
            namespace: ns.into(),
            key: key.into(),
        });
        let resp = dispatch_vault(&plugin, "secret_delete", &del_payload).unwrap();
        let del = secrets_proto::DeleteSecretResponse::decode(resp.as_slice()).unwrap();
        assert!(del.deleted);

        // Step 5: list with include_deleted=true — must surface the entry,
        // and the listed namespace must round-trip to the original string.
        let list_payload = encode(&secrets_proto::ListSecretsRequest {
            namespace: Some(ns.into()),
            include_deleted: true,
        });
        let resp = dispatch_vault(&plugin, "secret_list", &list_payload).unwrap();
        let list = secrets_proto::ListSecretsResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(list.count, 1);
        assert_eq!(list.secrets[0].namespace, ns);
        assert!(list.secrets[0].deleted);

        // Step 6: restore — must clear the tombstone.
        let restore_payload = encode(&secrets_proto::RestoreSecretRequest {
            namespace: ns.into(),
            key: key.into(),
        });
        let resp = dispatch_vault(&plugin, "secret_restore", &restore_payload).unwrap();
        let restored = secrets_proto::RestoreSecretResponse::decode(resp.as_slice()).unwrap();
        assert!(restored.restored);

        // Step 7: get post-restore — must return v2 again.
        let resp = dispatch_vault(&plugin, "secret_get", &get_payload).unwrap();
        let got = secrets_proto::GetSecretResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(got.value, "v2");

        // Step 8: delete_secret_version on a non-current version (v1).
        let dv_payload = encode(&secrets_proto::DeleteSecretVersionRequest {
            namespace: ns.into(),
            key: key.into(),
            version: 1,
        });
        let resp = dispatch_vault(&plugin, "secret_delete_version", &dv_payload).unwrap();
        let dv = secrets_proto::DeleteSecretVersionResponse::decode(resp.as_slice()).unwrap();
        assert!(dv.deleted);

        // Step 9: list_versions — only v2 should remain.
        let resp = dispatch_vault(&plugin, "secret_list_versions", &lv_payload).unwrap();
        let lv = secrets_proto::ListSecretVersionsResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(lv.count, 1);
        assert_eq!(lv.versions[0].version, 2);
    }

    // ── Migration verification — reads real Dropbox data ──────────
    //
    // Gated on VAULT_VERIFY_DIR env var. When set, it must point to
    // a directory whose `vault/` subdirectory contains:
    //   vault-meta.redb, content/, master.key
    //
    // Usage:
    //   VAULT_VERIFY_DIR=~/Dropbox/password-agent/nexus-vault-data \
    //     cargo test -p nexus-vault verify_migration -- --nocapture

    /// Open an existing vault data directory (read-only verification).
    fn existing_plugin(data_dir: &std::path::Path) -> VaultPlugin {
        let vault_dir = data_dir.join("vault");
        assert!(
            vault_dir.join("vault-meta.redb").exists(),
            "vault-meta.redb not found in {vault_dir:?}"
        );
        assert!(
            vault_dir.join("content").exists(),
            "content/ not found in {vault_dir:?}"
        );
        assert!(
            vault_dir.join("master.key").exists(),
            "master.key not found in {vault_dir:?}"
        );

        let kernel = Arc::new(kernel::kernel::Kernel::new());
        let meta_path = vault_dir.join("vault-meta.redb");
        kernel
            .set_metastore_path(meta_path.to_str().unwrap())
            .unwrap();

        let content_dir = vault_dir.join("content");
        let backend: Arc<dyn kernel::abc::object_store::ObjectStore> = Arc::new(
            backends::storage::path_local::PathLocalBackend::new(&content_dir, true).unwrap(),
        );
        let backend_name = backend.name().to_string();
        kernel
            .sys_setattr(
                "/vault",
                2,
                &backend_name,
                Some(backend),
                None,
                None,
                "memory",
                "root",
                false,
                0,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )
            .unwrap();

        let master_key_path = vault_dir.join("master.key");
        let master_key =
            services::password_vault::crypto::load_or_create_master_key(&master_key_path).unwrap();

        let secrets_svc =
            GenericSecretsServiceImpl::new_on_existing_mount(kernel, "/vault", master_key).unwrap();
        let svc = PasswordVaultServiceImpl::new_with_secrets(secrets_svc.clone());

        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();

        VaultPlugin {
            svc,
            secrets_svc,
            rt,
        }
    }

    #[test]
    fn verify_migration_list_and_get() {
        let data_dir = match std::env::var("VAULT_VERIFY_DIR") {
            Ok(d) => std::path::PathBuf::from(d),
            Err(_) => return, // skip when env var not set
        };

        let plugin = existing_plugin(&data_dir);

        // List all password entries.
        let list_payload = encode(&ListEntriesRequest {
            query: String::new(),
            limit: 0,
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "list_entries", &list_payload).unwrap();
        let list_resp = ListEntriesResponse::decode(resp_bytes.as_slice()).unwrap();

        println!(
            "=== Migration verification ===\n\
             Total entries: {}\n\
             Matched: {}",
            list_resp.total_in_vault, list_resp.matched,
        );
        assert!(
            list_resp.total_in_vault > 0,
            "expected migrated entries, got 0"
        );

        // Print first 10 titles for visual inspection.
        for (i, e) in list_resp.entries.iter().take(10).enumerate() {
            println!("  [{:>3}] {}", i + 1, e.title);
        }
        if list_resp.entries.len() > 10 {
            println!("  ... and {} more", list_resp.entries.len() - 10);
        }

        // Spot-check: get the first entry and verify it decrypts.
        let first_title = &list_resp.entries[0].title;
        let get_payload = encode(&GetEntryRequest {
            title: first_title.clone(),
            version: None,
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "get_entry", &get_payload).unwrap();
        let get_resp = GetEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        let entry = get_resp.entry.unwrap();
        println!(
            "\nSpot-check: title={:?}, username={:?}, has_password={}, version={}",
            entry.title,
            entry.username,
            entry.password.is_some(),
            get_resp.version,
        );
        assert!(
            entry.password.is_some() || entry.username.is_some(),
            "decrypted entry should have some data"
        );

        // Also check total including soft-deleted via generic secrets API.
        let all_payload = encode(&secrets_proto::ListSecretsRequest {
            namespace: Some("passwords".into()),
            include_deleted: true,
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_list", &all_payload).unwrap();
        let all_resp = secrets_proto::ListSecretsResponse::decode(resp_bytes.as_slice()).unwrap();
        let deleted_count = all_resp.secrets.iter().filter(|s| s.deleted).count();
        println!(
            "\nTotal (including deleted): {}, deleted: {}",
            all_resp.count, deleted_count,
        );

        println!("\n=== Migration verification PASSED ===");
    }

    // ── Phase P bytes-level gRPC dispatch ─────────────────────────────

    #[test]
    fn grpc_path_routes_generic_secrets_put_get() {
        let (_dir, plugin) = fresh_plugin();

        let put = encode(&secrets_proto::PutSecretRequest {
            namespace: "provider:openai".into(),
            key: "api_key".into(),
            value: "sk-grpc-routed".into(),
            description: None,
        });
        let resp = dispatch_vault(
            &plugin,
            "/nexus.secrets.v1.GenericSecretsService/PutSecret",
            &put,
        )
        .unwrap();
        let put_resp = secrets_proto::PutSecretResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(put_resp.metadata.as_ref().unwrap().current_version, 1);

        let get = encode(&secrets_proto::GetSecretRequest {
            namespace: "provider:openai".into(),
            key: "api_key".into(),
            version: None,
        });
        let resp = dispatch_vault(
            &plugin,
            "/nexus.secrets.v1.GenericSecretsService/GetSecret",
            &get,
        )
        .unwrap();
        let got = secrets_proto::GetSecretResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(got.value, "sk-grpc-routed");
    }

    #[test]
    fn grpc_path_routes_password_vault_put_entry() {
        let (_dir, plugin) = fresh_plugin();
        let payload = encode(&PutEntryRequest {
            entry: Some(entry("grpc-routed", "pw", None)),
            audit: None,
        });
        let resp = dispatch_vault(
            &plugin,
            "/nexus.password_vault.v1.PasswordVaultService/PutEntry",
            &payload,
        )
        .unwrap();
        let put_resp = PutEntryResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(put_resp.title, "grpc-routed");
    }

    #[test]
    fn grpc_path_routes_sealed_round_trip() {
        let (_dir, plugin) = fresh_plugin();

        let put = encode(&secrets_proto::PutSecretRequest {
            namespace: "signing-keys".into(),
            key: "dogfood".into(),
            value: "ed25519-bytes".into(),
            description: None,
        });
        dispatch_vault(
            &plugin,
            "/nexus.secrets.v1.GenericSecretsService/PutSecret",
            &put,
        )
        .unwrap();

        let get_sealed = encode(&secrets_proto::GetSecretRequest {
            namespace: "signing-keys".into(),
            key: "dogfood".into(),
            version: None,
        });
        let resp = dispatch_vault(
            &plugin,
            "/nexus.secrets.v1.GenericSecretsService/GetSecretSealed",
            &get_sealed,
        )
        .unwrap();
        let sealed = secrets_proto::GetSecretSealedResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(sealed.nonce.len(), 12);
        assert!(!sealed.ciphertext.is_empty());

        let put_sealed = encode(&secrets_proto::PutSecretSealedRequest {
            namespace: "signing-keys".into(),
            key: "dogfood-restored".into(),
            nonce: sealed.nonce,
            ciphertext: sealed.ciphertext,
            description: None,
        });
        dispatch_vault(
            &plugin,
            "/nexus.secrets.v1.GenericSecretsService/PutSecretSealed",
            &put_sealed,
        )
        .unwrap();

        let get = encode(&secrets_proto::GetSecretRequest {
            namespace: "signing-keys".into(),
            key: "dogfood-restored".into(),
            version: None,
        });
        let resp = dispatch_vault(
            &plugin,
            "/nexus.secrets.v1.GenericSecretsService/GetSecret",
            &get,
        )
        .unwrap();
        let got = secrets_proto::GetSecretResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(got.value, "ed25519-bytes");
    }

    #[test]
    fn grpc_path_unknown_service_returns_not_found() {
        let (_dir, plugin) = fresh_plugin();
        let err = dispatch_vault(&plugin, "/some.unknown.Svc/Foo", &[]).unwrap_err();
        assert_eq!(err, -1);
    }

    #[test]
    fn grpc_path_missing_method_returns_invalid_argument() {
        let (_dir, plugin) = fresh_plugin();
        let err = dispatch_vault(&plugin, "/no-method-separator-here", &[]).unwrap_err();
        assert_eq!(err, -2);
    }
}
