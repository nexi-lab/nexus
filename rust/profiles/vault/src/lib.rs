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

use std::path::PathBuf;
use std::sync::Arc;

use nexus_plugin_abi::{declare_service_plugin, KernelHandle};
use prost::Message;
use tonic::Request;

use services::password_vault::proto::password_vault_service_server::PasswordVaultService;
use services::password_vault::proto::*;
use services::password_vault::PasswordVaultServiceImpl;

/// Plugin state: the vault service plus a single-threaded tokio runtime
/// for driving the async gRPC trait methods (which are sync under the
/// hood but declared async for tonic compatibility).
struct VaultPlugin {
    svc: PasswordVaultServiceImpl,
    rt: tokio::runtime::Runtime,
}

fn create_vault(_kernel_handle: &KernelHandle) -> Box<VaultPlugin> {
    // Vault creates its own private Kernel instance — the host kernel
    // handle is available for coordination but vault storage is
    // self-contained (own metastore, own backend).
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

    let master_key_path = vault_dir.join("master.key");
    let svc =
        PasswordVaultServiceImpl::new_with_kernel(kernel, "/vault", &master_key_path, backend)
            .expect("open vault");

    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("build tokio runtime for vault plugin");

    tracing::info!(
        vault_dir = %vault_dir.display(),
        "vault plugin loaded"
    );

    Box::new(VaultPlugin { svc, rt })
}

fn dispatch_vault(plugin: &VaultPlugin, method: &str, payload: &[u8]) -> Result<Vec<u8>, i32> {
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
                .map_err(|e| status_to_plugin_error(e))?
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
                .map_err(|e| status_to_plugin_error(e))?
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
                .map_err(|e| status_to_plugin_error(e))?
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
                .map_err(|e| status_to_plugin_error(e))?
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
                .map_err(|e| status_to_plugin_error(e))?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        _ => Err(-1), // PluginResult::NotFound
    }
}

/// Map tonic Status codes to plugin result codes.
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
